from __future__ import annotations

import asyncio
import time
from typing import List

import arxiv

from .base import BaseRetriever, SearchResult

# ArXiv 要求请求间隔 ≥ 3 秒；429 时退避等待
_RETRY_WAITS = (10, 20, 40)   # 最多重试 3 次，等待时间（秒）


class ArxivRetriever(BaseRetriever):
    source_name = "arxiv"

    def __init__(self, sort: str = "Relevance"):
        assert sort in ("Relevance", "SubmittedDate"), f"Invalid sort: {sort}"
        self._sort = (
            arxiv.SortCriterion.SubmittedDate
            if sort == "SubmittedDate"
            else arxiv.SortCriterion.Relevance
        )

    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        return await asyncio.to_thread(self._sync_search_with_retry, query, max_results)

    def _sync_search_with_retry(self, query: str, max_results: int) -> List[SearchResult]:
        last_exc: Exception = RuntimeError("unreachable")
        for attempt, wait in enumerate((*_RETRY_WAITS, None)):
            try:
                return self._sync_search(query, max_results)
            except Exception as exc:
                last_exc = exc
                if "429" not in str(exc) or wait is None:
                    raise
                time.sleep(wait)
        raise last_exc

    def _sync_search(self, query: str, max_results: int) -> List[SearchResult]:
        client = arxiv.Client()
        search = arxiv.Search(
            query=query,
            max_results=max_results,
            sort_by=self._sort,
        )
        results = []
        for r in client.results(search):
            authors = [a.name for a in r.authors[:3]]
            if len(r.authors) > 3:
                authors.append("et al.")
            results.append(
                SearchResult(
                    title=r.title,
                    url=str(r.pdf_url),
                    snippet=r.summary,
                    source=self.source_name,
                    published=r.published.strftime("%Y-%m-%d") if r.published else None,
                    authors=authors,
                )
            )
        return results
