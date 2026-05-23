from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class SearchResult:
    title: str
    url: str                              # arxiv: pdf_url; local_file: absolute path
    snippet: str                          # arxiv: abstract; local_file: matched chunk
    source: str                           # "arxiv" / "local_file"
    published: Optional[str] = None       # 发表日期，格式 YYYY-MM-DD
    authors: Optional[List[str]] = None   # 作者列表（arxiv 提供，local_file 为 None）


class BaseRetriever(ABC):
    source_name: str = "base"

    @abstractmethod
    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        ...
