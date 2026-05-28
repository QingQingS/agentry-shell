"""
ResearchAgent —— 研究型 spoke：内部 ReAct 循环，LLM 自决检索 / 抓单页 / 广度调研。

设计（步5.5 后）：
  - 对外契约（步5 锁定）不变：入参 (prompt, context)；出 result.content（完整 markdown 报告）
    + metadata{status, summary}。
  - 内部 ReAct 循环：smart LLM 驱动，工具表见 agents/research_tools.py
    （search_papers / search_web / fetch_url / do_broad_survey）。
  - 不再有 mode/route 分发——「调研最新进展」/「读这篇 paper」/「找开源仓库」
    都由 LLM 看 prompt 自决用哪个工具。导师/学生类比。
  - status 三态：循环代码侧统计——所有工具调用 observation 都是「(...)」或「Error:」开头时
    标 degenerate；否则 ok。summary 取最终答复 markdown 的冒头段（_first_paragraph）。
  - 自然停止 = LLM 不发 tool_call；触顶 MAX_STEPS 时把当时已有 content 兜成结果。

回环兜底（复用 WikiAgent 风格）：
  - 错误转 observation（ToolRegistry.execute 永不向循环抛）。
  - 同一 (name, args) 重复 ≥ DUP_THRESHOLD 次时注入一次 nudge。
"""

from __future__ import annotations

import json
import re
import time
from datetime import date
from typing import AsyncIterator, List

from agents.research_tools import build_research_registry
from core.agent_interface import AgentEvent, AgentInterface
from core.llm import ChatMessage, get_llm
from core.retrievers import ArxivRetriever, BaseRetriever, TavilyRetriever

MAX_STEPS = 12          # 单次研究的 LLM 调用上限
DUP_THRESHOLD = 3       # 同一 (tool, args) 重复多少次触发 nudge

NUDGE_TEXT = (
    "提醒：你已多次执行同一个工具调用并得到相同结果，似乎卡住了。"
    "请改变策略——换工具/换参数，或者如果信息已够，直接用 markdown 写出最终报告（不要再调用工具）。"
)

SYSTEM_PROMPT_TEMPLATE = """你是研究助理。手头有这些工具可用：

- search_papers(query, max_results): 检索 ArXiv 论文，query 用英文效果更好。返回标题/作者/时间/摘要/链接。
- search_web(query, max_results): 检索网页（开源仓库、博客、代码示例等）。返回标题/链接/摘要。
- fetch_url(url, max_chars): 抓取指定 URL 的文本内容（HTML 去 tag 后返回）。用于读特定论文 / 看某个 README。
- do_broad_survey(topic, background): 对一个主题做广度调研（拆问 → 多源并发检索 → 综合），返回完整 markdown 报告。
  适合「调研 X 最新进展」这类宽问题——一次调用拿到结构化产出。

工作流程：
1. 看用户的研究任务，自主决定调用哪些工具。
   - 宽调研 → 通常一次 do_broad_survey 就够（也可以再补充 search/fetch 修补具体点）。
   - 找开源实现 → search_web 优先。
   - 读特定论文 / 看 README → fetch_url 抓全文。
   - 自己拆细问题分别查 → 多次 search_papers。
2. 完成后，直接用 markdown 写出最终研究报告：开头一段总览，分点论述，结论收尾。
   报告之后不要再调用工具——这是循环结束信号。

今天的日期：{today}
"""


class ResearchAgent(AgentInterface):
    name = "ResearchAgent"
    description = "研究型 spoke：内部 ReAct 循环，自决检索论文/网页/抓单页/广度调研。"

    async def run(self, task: str, **kwargs) -> AsyncIterator[AgentEvent]:
        # 生命周期与异常→error 由 core.runner 统一负责；这里只 yield 领域事件、失败时抛异常。
        # context 是新接口；旧 v1 OrchestratorAgent 仍传 background_context（cutover 时删）。
        background = (kwargs.get("context") or kwargs.get("background_context") or "").strip()

        fast = get_llm(tier="fast", config=self.config)
        smart = get_llm(tier="smart", config=self.config)
        retrievers = self._make_retrievers()
        registry = build_research_registry(fast, smart, retrievers)
        specs = registry.specs()

        yield AgentEvent(
            type="log",
            content=(
                f"smart={smart.model} 驱动循环；fast={fast.model} 备 do_broad_survey；"
                f"检索源={'+'.join(r.source_name for r in retrievers)}"
            ),
            metadata={"smart_model": smart.model, "fast_model": fast.model},
        )
        if background:
            yield AgentEvent(type="log", content="（携带上轮报告作背景）")

        today = date.today().isoformat()
        user_msg = task if not background else (
            f"{task}\n\n参考上一轮研究的已有结论（承接但不重复）：\n{background}"
        )
        messages: List[ChatMessage] = [
            ChatMessage(role="system", content=SYSTEM_PROMPT_TEMPLATE.format(today=today)),
            ChatMessage(role="user", content=user_msg),
        ]

        call_counts: dict = {}
        nudged = False
        retrieval_calls = 0      # 工具调用总数
        retrieval_hits = 0       # 非空（非 Error/非「(...)」开头）的工具结果数
        final_content = ""
        stopped_naturally = False

        for step in range(MAX_STEPS):
            t0 = time.monotonic()
            resp = await smart.chat(messages, tools=specs)
            dt = time.monotonic() - t0
            messages.append(ChatMessage(
                role="assistant",
                content=resp.content,
                tool_calls=resp.tool_calls,
                reasoning_content=resp.reasoning_content,
            ))

            think = (resp.reasoning_content or resp.content or "").strip()
            if think:
                yield AgentEvent(type="log", content=think, metadata={"trace": "think"})
            yield AgentEvent(
                type="log",
                content=f"第 {step + 1} 步 · {dt:.1f}s · +{resp.usage.total_tokens} tokens",
                metadata={"trace": "leaf"},
            )

            if not resp.tool_calls:
                final_content = resp.content
                stopped_naturally = True
                break

            for call in resp.tool_calls:
                sig = (call.name, json.dumps(call.arguments, sort_keys=True, ensure_ascii=False))
                call_counts[sig] = call_counts.get(sig, 0) + 1
                obs = await registry.execute(call)
                messages.append(ChatMessage(role="tool", content=obs, tool_call_id=call.id))
                yield AgentEvent(
                    type="log",
                    content=self._describe_action(call.name, call.arguments),
                    metadata={"trace": "action"},
                )
                yield AgentEvent(
                    type="log",
                    content=self._summarize_obs(call.name, obs),
                    metadata={"trace": "leaf"},
                )
                retrieval_calls += 1
                if not self._is_empty_obs(obs):
                    retrieval_hits += 1

            if not nudged and any(c >= DUP_THRESHOLD for c in call_counts.values()):
                messages.append(ChatMessage(role="user", content=NUDGE_TEXT))
                yield AgentEvent(type="log", content="检测到重复调用，已注入提醒（nudge）")
                nudged = True

        if not stopped_naturally:
            yield AgentEvent(type="log", content=f"达到步数上限（{MAX_STEPS}），提前结束。")
            if not final_content:
                final_content = "（因达步数上限未能写出完整报告。）"

        total = fast.cumulative_usage + smart.cumulative_usage
        yield AgentEvent(
            type="tokens",
            content=f"累计 input={total.input_tokens} output={total.output_tokens} total={total.total_tokens}",
            metadata={**total.to_dict(), "scope": "cumulative"},
        )

        # status：调过工具且全部空 → degenerate；否则 ok。无工具调用（纯对话答复）也算 ok。
        if retrieval_calls > 0 and retrieval_hits == 0:
            status, summary = "degenerate", "（未检索到相关结果）"
        else:
            status, summary = "ok", self._first_paragraph(final_content)
        yield AgentEvent(
            type="result",
            content=final_content,
            metadata={"status": status, "summary": summary},
        )

    # ---- 辅助 ----

    def _make_retrievers(self) -> List[BaseRetriever]:
        """解析 config.retriever（逗号分隔），返回 retriever 列表。

        测试通过 monkey-patch 这个方法注入 fake retrievers。"""
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

    @staticmethod
    def _is_empty_obs(obs: str) -> bool:
        """工具观察是否「无收获」：以 `(` 开头（约定的占位）或 `Error:` 开头。"""
        s = obs.lstrip()
        return s.startswith("(") or s.startswith("Error:")

    @staticmethod
    def _describe_action(name: str, args: dict) -> str:
        if name in ("search_papers", "search_web"):
            return f"{name}({args.get('query')!r})"
        if name == "fetch_url":
            return f"fetch_url({args.get('url')})"
        if name == "do_broad_survey":
            return f"do_broad_survey({args.get('topic')!r})"
        return f"{name}({json.dumps(args, ensure_ascii=False)})"

    @staticmethod
    def _summarize_obs(name: str, obs: str) -> str:
        if obs.startswith("Error:"):
            return f"✗ {obs}"
        if obs.startswith("("):
            return obs
        if name == "do_broad_survey":
            return f"得到 broad_survey 报告 / {len(obs)} 字"
        if name == "fetch_url":
            return f"抓到 {len(obs)} 字"
        # search_papers / search_web：用 `\n\n` 计条数
        n = obs.count("\n\n") + 1
        return f"得到 {n} 条结果 / {len(obs)} 字"

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
