from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import List, Union

from .base import BaseRetriever, SearchResult

_CHUNK_MAX = 500
_CHUNK_MIN = 100


class LocalFileRetriever(BaseRetriever):
    """
    Keyword-overlap retriever over local txt / md / pdf files.

    The index is built on first search() call (lazy, thread-safe within a
    single event loop since asyncio is single-threaded per coroutine).
    PDF parsing requires pymupdf (import fitz).
    """

    source_name = "local_file"

    def __init__(self, path: Union[str, Path, List[Union[str, Path]]]):
        if isinstance(path, (str, Path)):
            self._paths = [Path(path)]
        else:
            self._paths = [Path(p) for p in path]
        self._index: List[dict] = []  # {title, file_path, chunk, chunk_idx}
        self._loaded = False

    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        if not self._loaded:
            await asyncio.to_thread(self._build_index)
            self._loaded = True
        return self._keyword_search(query, max_results)

    # ── index building (runs in thread) ─────────────────────────────────

    def _build_index(self) -> None:
        for path in self._paths:
            p = Path(path)
            files = (
                list(p.rglob("*.txt")) + list(p.rglob("*.md")) + list(p.rglob("*.pdf"))
                if p.is_dir()
                else [p]
            )
            for f in files:
                try:
                    for idx, chunk in enumerate(self._load_file(f)):
                        self._index.append(
                            {"title": f.name, "file_path": str(f), "chunk": chunk, "chunk_idx": idx}
                        )
                except Exception:
                    pass  # skip unreadable files silently

    def _load_file(self, path: Path) -> List[str]:
        if path.suffix.lower() == ".pdf":
            return self._load_pdf(path)
        return self._chunk_text(path.read_text(encoding="utf-8", errors="ignore"))

    def _load_pdf(self, path: Path) -> List[str]:
        import fitz  # pymupdf

        doc = fitz.open(str(path))
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        return self._chunk_text(text)

    def _chunk_text(self, text: str) -> List[str]:
        raw = [c.strip() for c in text.split("\n\n") if c.strip()]

        # merge short paragraphs into buffer until buffer >= _CHUNK_MIN
        merged: List[str] = []
        buf = ""
        for para in raw:
            if not buf:
                buf = para
            elif len(buf) < _CHUNK_MIN:
                buf = buf + "\n\n" + para
            else:
                merged.append(buf)
                buf = para
        if buf:
            merged.append(buf)

        # split chunks that exceed _CHUNK_MAX
        chunks: List[str] = []
        for chunk in merged:
            if len(chunk) <= _CHUNK_MAX:
                chunks.append(chunk)
            else:
                for i in range(0, len(chunk), _CHUNK_MAX):
                    chunks.append(chunk[i : i + _CHUNK_MAX])
        return chunks

    # ── retrieval ────────────────────────────────────────────────────────

    def _keyword_search(self, query: str, max_results: int) -> List[SearchResult]:
        query_tokens = set(re.findall(r"\w+", query.lower()))
        if not query_tokens:
            return []

        scored: List[tuple] = []
        for item in self._index:
            chunk_tokens = set(re.findall(r"\w+", item["chunk"].lower()))
            overlap = len(query_tokens & chunk_tokens)
            if overlap:
                scored.append((overlap / len(query_tokens), item))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            SearchResult(
                title=item["title"],
                url=item["file_path"],
                snippet=item["chunk"],
                source=self.source_name,
            )
            for _, item in scored[:max_results]
        ]
