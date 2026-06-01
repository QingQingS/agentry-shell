from __future__ import annotations

import asyncio
import logging
import socket
import time
import urllib.error
from typing import List

import arxiv

from .base import BaseRetriever, SearchResult

_log = logging.getLogger(__name__)

# ArXiv 要求请求间隔 ≥ 3 秒；瞬时错误（429/503/连接）时退避等待
_RETRY_WAITS = (10, 20, 40)   # 最多重试 3 次，等待时间（秒）

# 可重试的瞬时错误：限流 / 服务端 5xx / 连接层问题。
# arxiv 库把 HTTP 错误包成普通异常，故同时按异常类型与消息子串判定。
_RETRIABLE_TOKENS = ("429", "503", "502", "500", "connection", "timeout", "timed out", "temporarily")


def _is_retriable(exc: Exception) -> bool:
    if isinstance(exc, (urllib.error.URLError, ConnectionError, TimeoutError, socket.timeout)):
        return True
    return any(tok in str(exc).lower() for tok in _RETRIABLE_TOKENS)


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
                if not _is_retriable(exc) or wait is None:
                    raise
                _log.warning(
                    "arxiv 检索瞬时失败（%s），第 %d/%d 次退避 %ds 后重试：%s",
                    type(exc).__name__, attempt + 1, len(_RETRY_WAITS), wait, exc,
                )
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
