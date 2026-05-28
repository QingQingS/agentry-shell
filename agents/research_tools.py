"""
ResearchAgent 私有工具层 —— spoke 内部 ReAct 循环用的工具。

设计：
  - 4 个工具：原子检索（search_papers/search_web/fetch_url）+ 复合（do_broad_survey）。
  - 复用 core/tools.py 的 Tool ABC / ToolRegistry（错误兜底机制相同）。
  - **不进** Coordinator 的 AgentRegistry——这些是 ResearchAgent 私有细节。
  - fetch_url 用 stdlib（urllib + re），无新依赖；同步 I/O 经 asyncio.to_thread 不阻塞。
  - do_broad_survey 内部沿用「拆问 → 多源并发检索 → 综合」流程；其 fast/smart 调用
    仍走 on_tokens 回调（保留累计统计），但不 stream chunk 出去——LLM 看的是 observation
    字符串，细粒度可观测性留给后续。

degenerate 信号：原子工具空检索时返回「(未检索到相关论文)」/「(未检索到相关结果)」；
do_broad_survey 所有子问题都空时返回「(未检索到相关资料：…)」开头的占位串。
ResearchAgent 的循环代码以「observation 是否以 `(` 或 `Error:` 开头」聚合判定 degenerate。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

from core.llm import ChatMessage
from core.llm.base import ToolSpec
from core.retrievers import BaseRetriever, SearchResult
from core.tools import Tool, ToolRegistry


def _format_results(results: List[SearchResult]) -> str:
    def _fmt(r: SearchResult) -> str:
        lines = [f"标题: {r.title}"]
        if r.published:
            lines.append(f"发表时间: {r.published}")
        if r.authors:
            lines.append(f"作者: {', '.join(r.authors)}")
        lines.append(f"摘要: {r.snippet}")
        lines.append(f"链接: {r.url}")
        return "\n".join(lines)
    return "\n\n".join(_fmt(r) for r in results)


class SearchPapersTool(Tool):
    spec = ToolSpec(
        name="search_papers",
        description="检索 ArXiv 论文。query 用英文效果更好。返回标题/作者/时间/摘要/链接。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索查询（英文效果更好）"},
                "max_results": {"type": "integer", "description": "返回数量上限", "default": 5},
            },
            "required": ["query"],
        },
    )

    def __init__(self, retriever: BaseRetriever):
        self.retriever = retriever

    async def execute(self, query: str, max_results: int = 5) -> str:
        results = await self.retriever.search(query, max_results=max_results)
        if not results:
            return "(未检索到相关论文)"
        return _format_results(results)


class SearchWebTool(Tool):
    spec = ToolSpec(
        name="search_web",
        description="检索网页（开源仓库、博客、代码示例等）。返回标题/链接/摘要。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索查询"},
                "max_results": {"type": "integer", "description": "返回数量上限", "default": 5},
            },
            "required": ["query"],
        },
    )

    def __init__(self, retriever: BaseRetriever):
        self.retriever = retriever

    async def execute(self, query: str, max_results: int = 5) -> str:
        results = await self.retriever.search(query, max_results=max_results)
        if not results:
            return "(未检索到相关结果)"
        return _format_results(results)


_HTML_BLOCK_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


class FetchUrlTool(Tool):
    spec = ToolSpec(
        name="fetch_url",
        description="抓取指定 URL 的文本内容（HTML 去 tag 后返回，最多 max_chars 字符）。",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的 URL（http/https）"},
                "max_chars": {"type": "integer", "description": "返回的字符上限", "default": 8000},
            },
            "required": ["url"],
        },
    )

    TIMEOUT = 15

    async def execute(self, url: str, max_chars: int = 8000) -> str:
        if not url.startswith(("http://", "https://")):
            return f"Error: URL 必须以 http:// 或 https:// 开头：{url}"
        try:
            text = await asyncio.to_thread(self._fetch_sync, url)
        except urllib.error.URLError as e:
            return f"Error: 抓取失败 {url}: {e}"
        except Exception as e:  # noqa: BLE001 — 工具不向循环抛
            return f"Error: 抓取异常 {url}: {type(e).__name__}: {e}"
        if not text:
            return "(URL 抓取到空内容)"
        if len(text) > max_chars:
            text = text[:max_chars] + "…（已截断）"
        return text

    def _fetch_sync(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research-agent)"})
        with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        text = _HTML_BLOCK_RE.sub("", raw)
        text = _HTML_TAG_RE.sub(" ", text)
        text = _WHITESPACE_RE.sub(" ", text).strip()
        return text


class DoBroadSurveyTool(Tool):
    """复合工具：「拆问 → 多源并发检索 → 综合」流程，返回完整 markdown 报告。

    封装原 ResearchAgent._run_survey 主体。LLM 想做宽调研时调一次此工具即可；
    也可选择多次原子 search 自己拆解——agentic 自决。
    """

    spec = ToolSpec(
        name="do_broad_survey",
        description=(
            "对一个主题做广度调研：拆问 → 多源并发检索 → 综合，返回结构化 markdown 报告。"
            "适合「调研 X 最新进展」这类宽问题。background 可选：传入上轮报告时新报告会聚焦未覆盖角度。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "研究主题"},
                "background": {"type": "string", "description": "可选：上一轮报告（让新调研聚焦未覆盖角度）", "default": ""},
            },
            "required": ["topic"],
        },
    )

    NUM_SUB_QUESTIONS = 3
    RESULTS_PER_QUESTION = 4

    def __init__(self, fast_llm, smart_llm, retrievers: List[BaseRetriever]):
        self.fast = fast_llm
        self.smart = smart_llm
        self.retrievers = retrievers

    async def execute(self, topic: str, background: str = "") -> str:
        resp = await self.fast.chat(self._decompose_messages(topic, background))
        sub_questions = self._parse_subquestions(resp.content) or [topic]

        summaries: List[Tuple[str, str]] = []
        any_hit = False
        for sq in sub_questions:
            raw = await asyncio.gather(
                *[r.search(sq, max_results=self.RESULTS_PER_QUESTION) for r in self.retrievers],
                return_exceptions=True,
            )
            batches = [b if not isinstance(b, Exception) else [] for b in raw]
            results = self._merge_results(batches)
            if not results:
                summaries.append((sq, "（未检索到相关论文）"))
                continue
            any_hit = True
            summary = await self.fast.chat(self._summarize_messages(sq, results))
            summaries.append((sq, summary.content))

        if not any_hit:
            return f"(未检索到相关资料：{len(sub_questions)} 个子问题全部空检索)"

        report_resp = await self.smart.chat(self._report_messages(topic, summaries, background))
        return report_resp.content

    # ---- prompt 构造 + 解析（从原 ResearchAgent 迁来）----

    def _decompose_messages(self, topic: str, background: str) -> List[ChatMessage]:
        system = (
            "你是研究助理。把用户的研究主题拆解成具体、可检索的英文子问题"
            "（ArXiv 论文检索用英文效果更好）。"
            f"只输出一个 JSON 数组，包含 {self.NUM_SUB_QUESTIONS} 个字符串，不要任何其它文字。"
            '例如：["sub question 1", "sub question 2", "sub question 3"]'
        )
        if background:
            system += "\n\n用户已有以下背景研究，请让子问题聚焦于背景未覆盖的新角度：\n" + background
        return [ChatMessage(role="system", content=system), ChatMessage(role="user", content=topic)]

    def _summarize_messages(self, sub_q: str, results: List[SearchResult]) -> List[ChatMessage]:
        return [
            ChatMessage(role="system", content=(
                "你是研究助理。根据提供的资料，用中文简洁总结针对该子问题的发现（2-4 句），"
                "并引用相关来源（使用原标题，不要翻译）。"
                "如有发表时间和作者信息，请在引用时一并标注。"
                "不要编造资料之外的内容。"
            )),
            ChatMessage(role="user", content=f"子问题：{sub_q}\n\n检索到的资料：\n{_format_results(results)}"),
        ]

    def _report_messages(self, topic: str, summaries: List[Tuple[str, str]], background: str) -> List[ChatMessage]:
        body = "\n\n".join(f"## 子问题 {i}:{sq}\n{s}" for i, (sq, s) in enumerate(summaries, 1))
        system = (
            "你是研究分析师。基于各子问题的发现，写一份结构化中文研究简报："
            "开头一段总览，然后分点综合各发现，最后给出结论。使用 Markdown 格式。"
        )
        if background:
            system += "\n\n参考以下上一轮研究的已有结论，承接但不要重复：\n" + background
        return [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=f"研究主题：{topic}\n\n各子问题发现：\n{body}"),
        ]

    @staticmethod
    def _merge_results(batches: List[List[SearchResult]]) -> List[SearchResult]:
        """多源结果合并，按 URL 去重，保持各源交叉排列以均衡来源。"""
        seen: set = set()
        merged: List[SearchResult] = []
        for items in zip(*[b for b in batches if b]):
            for r in items:
                if r.url not in seen:
                    seen.add(r.url)
                    merged.append(r)
        for batch in batches:
            for r in batch:
                if r.url not in seen:
                    seen.add(r.url)
                    merged.append(r)
        return merged

    @classmethod
    def _parse_subquestions(cls, text: str) -> List[str]:
        """先尝试解析 JSON 数组，失败则回退到按行切分（去掉项目符号/编号）。"""
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            try:
                arr = json.loads(m.group(0))
                qs = [str(x).strip() for x in arr if str(x).strip()]
                if qs:
                    return qs[: cls.NUM_SUB_QUESTIONS]
            except (json.JSONDecodeError, TypeError):
                pass
        lines = []
        for ln in text.splitlines():
            ln = re.sub(r"^\s*[-*\d.)\]]+\s*", "", ln).strip().strip('"').strip()
            if ln:
                lines.append(ln)
        return lines[: cls.NUM_SUB_QUESTIONS]


class SaveReportTool(Tool):
    """ResearchAgent 末轮调用以落盘最终报告到 reports/。

    跨 agent 数据通过文件系统传递（artifact-as-first-class）：本工具是 ResearchAgent
    唯一的产出落点；Coordinator 拿到 artifact 路径后用 stage_files 把它转到
    wiki/staging/，再派 wiki_curator。

    保护：
    - filename 必须以 .md 结尾，且不含路径分隔（/ \\）或 ..
    - 同名冲突自动追加时间戳后缀
    - 父目录自动创建
    """

    DEFAULT_ROOT = "reports"
    OBS_PATTERN = re.compile(r"^已保存 (\S+)（\d+ 字符）$")

    spec = ToolSpec(
        name="save_report",
        description=(
            "把最终研究报告落盘到 reports/<filename>。filename 必须以 .md 结尾、"
            "不含路径分隔或 ..；同名冲突时自动追加时间戳。"
            "完成本调用后请直接结束（下一轮不要再调任何工具）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "报告文件名（如 rl-survey.md）"},
                "content": {"type": "string", "description": "报告完整 markdown 内容"},
            },
            "required": ["filename", "content"],
        },
    )

    def __init__(self, root: Optional[Path] = None):
        self.root = (Path(root) if root else Path(self.DEFAULT_ROOT)).resolve()

    async def execute(self, filename: str, content: str) -> str:
        if not filename:
            return "Error: filename 不能为空"
        if "/" in filename or "\\" in filename or ".." in filename:
            return f"Error: filename 不允许包含路径分隔或 ..: {filename}"
        if not filename.endswith(".md"):
            return f"Error: filename 必须以 .md 结尾: {filename}"
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / filename
        if target.exists():
            ts = time.strftime("%Y%m%d-%H%M%S")
            target = self.root / f"{target.stem}-{ts}.md"
        target.write_text(content, encoding="utf-8")
        display = self._display_path(target)
        return f"已保存 {display}（{len(content)} 字符）"

    @staticmethod
    def _display_path(p: Path) -> str:
        """优先用相对 cwd 的路径，便于下游 stage_files 引用。"""
        try:
            return str(p.relative_to(Path.cwd()))
        except ValueError:
            return str(p)

    @classmethod
    def parse_obs_path(cls, obs: str) -> Optional[str]:
        """从 obs 文本提取 save_report 实际写入的路径（供 ResearchAgent 循环读取）。"""
        m = cls.OBS_PATTERN.match(obs.strip())
        return m.group(1) if m else None


def build_research_registry(
    fast_llm,
    smart_llm,
    retrievers: List[BaseRetriever],
    reports_root: Optional[Path] = None,
) -> ToolRegistry:
    """造 ResearchAgent 私有工具注册表。

    retrievers 必须至少一个（ResearchAgent._make_retrievers 兜底 ArxivRetriever）。按 source
    分配给原子工具（arxiv → search_papers，tavily → search_web）；do_broad_survey 用全部并发。
    save_report 写 reports_root/<filename>（默认 ./reports/）。
    """
    arxiv_r = next((r for r in retrievers if r.source_name == "arxiv"), retrievers[0])
    tavily_r = next((r for r in retrievers if r.source_name == "tavily"), arxiv_r)
    return ToolRegistry([
        SearchPapersTool(arxiv_r),
        SearchWebTool(tavily_r),
        FetchUrlTool(),
        DoBroadSurveyTool(fast_llm, smart_llm, retrievers),
        SaveReportTool(reports_root),
    ])
