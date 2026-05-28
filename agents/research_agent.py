"""
ResearchAgent —— 研究型 Agent（多 mode 编排）。

mode 由调用层（OrchestratorAgent）下达，决定检索机制与输出形状；
mode 枚举 ResearchMode 在此定义，作为 intent 层与本 Agent 的共享契约。

  survey       广度调研（默认，现行为）：
                 主问题 → [fast] 拆英文子问题 → 多检索源并发检索/去重
                       → [fast] 逐子问题总结 → [smart] 汇总结构化报告（流式）
  paper_lookup 单篇/单一目标（ArXiv）：单次检索 → [smart] 针对性综述
  code_search  找开源实现（Tavily Web）：单次检索 → [smart] 列出仓库/实现

background_context（可选 kwarg）：上一轮报告，注入提示词作背景，子问题/报告聚焦
未覆盖角度、不重复已有结论。

检索源：survey 用 config.retriever（逗号分隔多源并发）；paper_lookup 固定 ArXiv；
code_search 固定 Tavily。
"""

from __future__ import annotations

import asyncio
import json
import re
from enum import Enum
from typing import AsyncIterator, List, Tuple

from core.agent_interface import AgentEvent, AgentInterface
from core.llm import ChatMessage, LLMResponse, get_llm
from core.retrievers import ArxivRetriever, BaseRetriever, SearchResult, TavilyRetriever


class ResearchMode(str, Enum):
    SURVEY = "survey"
    PAPER_LOOKUP = "paper_lookup"
    CODE_SEARCH = "code_search"


class ResearchAgent(AgentInterface):
    name = "ResearchAgent"
    description = "研究型 Agent：按 mode（survey/paper_lookup/code_search）编排检索与报告。"

    NUM_SUB_QUESTIONS = 3
    RESULTS_PER_QUESTION = 4
    FOCUSED_RESULTS = 6

    @staticmethod
    def _normalize_mode(value) -> ResearchMode:
        try:
            return ResearchMode(value)
        except (ValueError, TypeError):
            return ResearchMode.SURVEY

    def _make_retrievers(self) -> List[BaseRetriever]:
        """解析 config.retriever（逗号分隔），返回检索器列表（survey 用）。"""
        names = [
            n.strip().lower()
            for n in getattr(self.config, "retriever", "arxiv").split(",")
            if n.strip()
        ]
        retrievers: List[BaseRetriever] = []
        for name in names:
            if name == "tavily":
                retrievers.append(
                    TavilyRetriever(api_key=getattr(self.config, "tavily_api_key", None))
                )
            else:
                retrievers.append(ArxivRetriever())
        return retrievers or [ArxivRetriever()]

    def _retrievers_for_mode(self, mode: ResearchMode) -> List[BaseRetriever]:
        if mode == ResearchMode.PAPER_LOOKUP:
            return [ArxivRetriever()]
        if mode == ResearchMode.CODE_SEARCH:
            return [TavilyRetriever(api_key=getattr(self.config, "tavily_api_key", None))]
        return self._make_retrievers()

    def _merge_results(self, batches: List[List[SearchResult]]) -> List[SearchResult]:
        """多源结果合并，按 URL 去重，保持各源交叉排列以均衡来源。"""
        seen: set = set()
        merged: List[SearchResult] = []
        for items in zip(*[b for b in batches if b]):   # 交叉：arxiv[0], tavily[0], arxiv[1]...
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

    async def run(self, task: str, **kwargs) -> AsyncIterator[AgentEvent]:
        # 生命周期与异常→error 事件由 core.runner 统一负责；这里只 yield 领域事件、失败时抛异常。
        mode = self._normalize_mode(kwargs.get("mode"))
        # 新接口走 context；旧 v1 OrchestratorAgent 仍传 background_context（cutover 时删）。
        background = (kwargs.get("context") or kwargs.get("background_context") or "").strip()

        fast = get_llm(tier="fast", config=self.config)
        smart = get_llm(tier="smart", config=self.config)
        retrievers = self._retrievers_for_mode(mode)
        source_names = "+".join(r.source_name for r in retrievers)

        yield AgentEvent(
            type="log",
            content=f"mode={mode.value}，fast={fast.model}/smart={smart.model}，检索源={source_names}",
            metadata={"mode": mode.value, "fast_model": fast.model, "smart_model": smart.model, "retriever": source_names},
        )
        if background:
            yield AgentEvent(type="log", content="（携带上轮报告作背景）")

        if mode == ResearchMode.SURVEY:
            async for ev in self._run_survey(task, background, fast, smart, retrievers):
                yield ev
        else:
            async for ev in self._run_focused(task, background, mode, smart, retrievers[0]):
                yield ev

    # ── survey：广度调研（现行为 + 背景注入）────────────────────────────────

    async def _run_survey(
        self, task: str, background: str, fast, smart, retrievers: List[BaseRetriever]
    ) -> AsyncIterator[AgentEvent]:
        # 1) 拆解子问题
        yield AgentEvent(type="log", content=f"拆解研究问题：{task}")
        resp = await fast.chat(self._decompose_messages(task, background))
        yield self._tokens_event(resp)
        sub_questions = self._parse_subquestions(resp.content) or [task]
        yield AgentEvent(
            type="log",
            content=f"得到 {len(sub_questions)} 个子问题：" + " | ".join(sub_questions),
            metadata={"sub_questions": sub_questions},
        )

        # 2) 逐子问题：多源并发检索 + 合并去重 + 总结
        summaries: List[Tuple[str, str]] = []
        for i, sq in enumerate(sub_questions, 1):
            yield AgentEvent(type="log", content=f"[{i}/{len(sub_questions)}] 检索：{sq}")

            raw = await asyncio.gather(
                *[r.search(sq, max_results=self.RESULTS_PER_QUESTION) for r in retrievers],
                return_exceptions=True,
            )
            batches: List[List[SearchResult]] = []
            for retriever, outcome in zip(retrievers, raw):
                if isinstance(outcome, Exception):
                    yield AgentEvent(
                        type="log",
                        content=f"  [{retriever.source_name}] 检索失败，跳过：{outcome}",
                    )
                    batches.append([])
                else:
                    batches.append(outcome)
                    if len(retrievers) > 1:
                        yield AgentEvent(type="log", content=f"  [{retriever.source_name}] {len(outcome)} 条")

            results = self._merge_results(batches)
            total_label = f"合计 {len(results)} 条" + ("（去重后）" if len(retrievers) > 1 else "")
            yield AgentEvent(type="log", content=f"  {total_label}")

            if not results:
                summaries.append((sq, "（未检索到相关论文）"))
                continue

            resp = await fast.chat(self._summarize_messages(sq, results))
            yield self._tokens_event(resp)
            summaries.append((sq, resp.content))
            yield AgentEvent(type="log", content=f"  已总结子问题 {i}")

        # 3) 汇总最终报告（流式推送）
        yield AgentEvent(type="log", content="汇总最终报告…")
        report_msgs = self._report_messages(task, summaries, background)
        full_report = ""
        async for chunk in smart.chat_stream(report_msgs):
            full_report += chunk
            yield AgentEvent(type="stream", content=chunk)

        async for ev in self._final_token_events(fast, smart):
            yield ev

        # status 三态：所有子问题都空检索 → degenerate；否则 ok。summary 取报告冒头段（免 LLM 自摘要）。
        degenerate = bool(summaries) and all(s == "（未检索到相关论文）" for _, s in summaries)
        if degenerate:
            status, summary = "degenerate", "（未检索到相关结果）"
        else:
            status, summary = "ok", self._first_paragraph(full_report)
        yield AgentEvent(
            type="result",
            content=full_report,
            metadata={"status": status, "summary": summary},
        )

    # ── paper_lookup / code_search：单源单查询 + 针对性报告 ──────────────────

    async def _run_focused(
        self, task: str, background: str, mode: ResearchMode, smart, retriever: BaseRetriever
    ) -> AsyncIterator[AgentEvent]:
        query = self._focused_query(task, mode)
        yield AgentEvent(type="log", content=f"[{mode.value}] 检索：{query}")
        results = await retriever.search(query, max_results=self.FOCUSED_RESULTS)
        yield AgentEvent(
            type="log",
            content=f"  [{retriever.source_name}] 得到 {len(results)} 条",
            metadata={"count": len(results)},
        )
        if not results:
            yield AgentEvent(type="result", content="（未检索到相关结果）")
            return

        report_msgs = self._focused_report_messages(task, results, background, mode)
        full_report = ""
        async for chunk in smart.chat_stream(report_msgs):
            full_report += chunk
            yield AgentEvent(type="stream", content=chunk)

        usage = smart.cumulative_usage
        yield AgentEvent(
            type="tokens",
            content=f"input={usage.input_tokens} output={usage.output_tokens} total={usage.total_tokens}",
            metadata={**usage.to_dict(), "provider": smart.provider_name, "model": smart.model, "scope": "cumulative"},
        )
        yield AgentEvent(type="result", content=full_report)

    def _focused_query(self, task: str, mode: ResearchMode) -> str:
        if mode == ResearchMode.CODE_SEARCH:
            return f"{task} github open source implementation code"
        return task

    # ── prompt 构造 ──────────────────────────────────────────────────────

    def _format_results(self, results: List[SearchResult]) -> str:
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

    def _decompose_messages(self, task: str, background: str = "") -> List[ChatMessage]:
        system = (
            "你是研究助理。把用户的研究主题拆解成具体、可检索的英文子问题"
            "（ArXiv 论文检索用英文效果更好）。"
            f"只输出一个 JSON 数组，包含 {self.NUM_SUB_QUESTIONS} 个字符串，不要任何其它文字。"
            '例如：["sub question 1", "sub question 2", "sub question 3"]'
        )
        if background:
            system += (
                "\n\n用户已有以下背景研究，请让子问题聚焦于背景未覆盖的新角度：\n" + background
            )
        return [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=task),
        ]

    def _summarize_messages(self, sub_question: str, results: List[SearchResult]) -> List[ChatMessage]:
        return [
            ChatMessage(
                role="system",
                content=(
                    "你是研究助理。根据提供的资料，用中文简洁总结针对该子问题的发现（2-4 句），"
                    "并引用相关来源（使用原标题，不要翻译）。"
                    "如有发表时间和作者信息，请在引用时一并标注。"
                    "不要编造资料之外的内容。"
                ),
            ),
            ChatMessage(role="user", content=f"子问题：{sub_question}\n\n检索到的资料：\n{self._format_results(results)}"),
        ]

    def _report_messages(self, task: str, summaries: List[Tuple[str, str]], background: str = "") -> List[ChatMessage]:
        body = "\n\n".join(
            f"## 子问题 {i}：{sq}\n{summ}" for i, (sq, summ) in enumerate(summaries, 1)
        )
        system = (
            "你是研究分析师。基于各子问题的发现，写一份结构化中文研究简报："
            "开头一段总览，然后分点综合各发现，最后给出结论。使用 Markdown 格式。"
        )
        if background:
            system += "\n\n参考以下上一轮研究的已有结论，承接但不要重复：\n" + background
        return [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=f"研究主题：{task}\n\n各子问题发现：\n{body}"),
        ]

    def _focused_report_messages(
        self, task: str, results: List[SearchResult], background: str, mode: ResearchMode
    ) -> List[ChatMessage]:
        if mode == ResearchMode.CODE_SEARCH:
            system = (
                "你是研究助理。基于检索资料，用中文列出与目标相关的开源实现 / GitHub 仓库，"
                "每项给出：名称、链接、一句话简介。按相关度排序。不要编造资料之外的内容。"
                "使用 Markdown 列表格式。"
            )
        else:  # PAPER_LOOKUP
            system = (
                "你是研究助理。基于检索资料，用中文针对该目标做一份简明综述："
                "核心贡献、方法要点、关键结论，并引用来源（原标题、作者、发表时间）。"
                "不要编造资料之外的内容。使用 Markdown 格式。"
            )
        if background:
            system += "\n\n参考以下上一轮研究的已有结论，承接但不要重复：\n" + background
        return [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=f"目标：{task}\n\n检索到的资料：\n{self._format_results(results)}"),
        ]

    # ── 工具方法 ─────────────────────────────────────────────────────────

    async def _final_token_events(self, fast, smart) -> AsyncIterator[AgentEvent]:
        smart_usage = smart.cumulative_usage
        yield AgentEvent(
            type="tokens",
            content=f"input={smart_usage.input_tokens} output={smart_usage.output_tokens} total={smart_usage.total_tokens}",
            metadata={**smart_usage.to_dict(), "provider": smart.provider_name, "model": smart.model},
        )
        total = fast.cumulative_usage + smart.cumulative_usage
        yield AgentEvent(
            type="tokens",
            content=f"累计 input={total.input_tokens} output={total.output_tokens} total={total.total_tokens}",
            metadata={**total.to_dict(), "scope": "cumulative"},
        )

    def _tokens_event(self, resp: LLMResponse) -> AgentEvent:
        return AgentEvent(
            type="tokens",
            content=(
                f"input={resp.usage.input_tokens}  "
                f"output={resp.usage.output_tokens}  "
                f"total={resp.usage.total_tokens}"
            ),
            metadata={**resp.usage.to_dict(), "provider": resp.provider, "model": resp.model},
        )

    @staticmethod
    def _first_paragraph(text: str) -> str:
        """报告冒头一段：跳过空行和 # 标题行，取第一段连续内容；找不到则回退取前 200 字。"""
        para: List[str] = []
        for ln in text.strip().splitlines():
            s = ln.strip()
            if not s:
                if para:
                    break
                continue
            if s.startswith("#"):
                if para:
                    break
                continue
            para.append(s)
        if para:
            return " ".join(para)
        return re.sub(r"\s+", " ", text).strip()[:200]

    def _parse_subquestions(self, text: str) -> List[str]:
        """先尝试解析 JSON 数组，失败则回退到按行切分（去掉项目符号/编号）。"""
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            try:
                arr = json.loads(m.group(0))
                qs = [str(x).strip() for x in arr if str(x).strip()]
                if qs:
                    return qs[: self.NUM_SUB_QUESTIONS]
            except (json.JSONDecodeError, TypeError):
                pass

        lines = []
        for ln in text.splitlines():
            ln = re.sub(r"^\s*[-*\d.)\]]+\s*", "", ln).strip().strip('"').strip()
            if ln:
                lines.append(ln)
        return lines[: self.NUM_SUB_QUESTIONS]
