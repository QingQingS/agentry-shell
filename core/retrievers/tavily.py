"""
Tavily Web 检索器。

调用 Tavily Search API 检索实时互联网内容。
需在 .env 配置 TAVILY_API_KEY（从 https://app.tavily.com/ 获取）。

依赖：httpx（FastAPI 生态已有，无需额外安装）
"""

from __future__ import annotations

import os
from typing import List, Optional

import httpx

from .base import BaseRetriever, SearchResult


_API_URL = "https://api.tavily.com/search"


class TavilyRetriever(BaseRetriever):
    source_name = "tavily"

    def __init__(
        self,
        api_key: Optional[str] = None,
        search_depth: str = "basic",    # "basic" 足够日常使用；"advanced" 更深但更慢
        topic: str = "general",         # "general" | "news"
    ):
        self._api_key = api_key or os.getenv("TAVILY_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "Tavily API key 未配置。请在 .env 设置 TAVILY_API_KEY=tvly-..."
            )
        self._search_depth = search_depth
        self._topic = topic

    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        payload = {
            "api_key": self._api_key,
            "query": query,
            "search_depth": self._search_depth,
            "topic": self._topic,
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(_API_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()

        results = []
        for item in data.get("results", []):
            published = item.get("published_date")
            # Tavily 返回 "2024-01-15T..." 格式；只取日期部分
            if published and "T" in published:
                published = published[:10]
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", ""),
                    source=self.source_name,
                    published=published or None,
                )
            )
        return results
