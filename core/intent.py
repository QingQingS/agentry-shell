"""
意图分类器 —— 阶段三连续对话的路由判断。

把「用户新输入 + 会话上下文」一次性分类成三个正交字段（详见 NOTES.md 2026-05-22）：
    route          "research" | "chat"   —— 要不要新检索 → 选 ResearchAgent / ChatAgent
    mode           ResearchMode 值        —— 仅 research 有意义，决定 ResearchAgent 内部分支
    target         可直接检索的查询词     —— research 时填，chat 时可空
    carry_context  bool                   —— 这一轮要不要带上轮报告作背景

设计约束：
    - 一次 fast LLM 调用完成全部字段，严格 JSON 输出
    - 无上下文（首轮）跳过 LLM，直接返回 research/survey
    - 解析失败降级为 research/survey（最安全的默认）
    - ResearchMode 是跨层契约，定义在 agents.research_agent（agent 拥有），此处 import
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import List, Optional

from agents.research_agent import ResearchMode
from core.llm import ChatMessage
from core.session import RecentContext

_ROUTES = {"research", "chat"}
_MODES = {m.value for m in ResearchMode}
_RECENT_TURNS_IN_PROMPT = 3
_RESPONSE_SNIPPET = 120


@dataclass
class IntentResult:
    route: str
    mode: str
    target: str
    carry_context: bool


def _degraded(target: str) -> IntentResult:
    return IntentResult(route="research", mode=ResearchMode.SURVEY.value, target=target, carry_context=False)


async def classify_intent(
    user_input: str, session_context: Optional[RecentContext], llm
) -> IntentResult:
    """分类用户输入。无上下文跳过 LLM；解析失败降级为 research/survey。"""
    if session_context is None or (not session_context.reports and not session_context.turns):
        return _degraded(user_input)

    resp = await llm.chat(_build_messages(user_input, session_context))
    return _parse(resp.content, fallback_target=user_input)


def _build_messages(user_input: str, ctx: RecentContext) -> List[ChatMessage]:
    reports_block = "\n".join(
        f"- 主题：{r.topic}；摘要：{r.description}" for r in ctx.reports
    ) or "（无）"
    turns_block = "\n".join(
        f"用户：{t.user_input}\n助理：{t.agent_response[:_RESPONSE_SNIPPET]}"
        for t in ctx.turns[-_RECENT_TURNS_IN_PROMPT:]
    ) or "（无）"

    system = (
        "你是对话路由器。根据已有研究背景和最近对话，判断用户新输入应如何处理，"
        "只输出一个 JSON 对象，不要任何其它文字。字段：\n"
        '  "route": "chat" 或 "research"。chat = 能用已有报告直接回答的追问，无需新检索；'
        "research = 需要发起新的检索。\n"
        '  "mode": "survey" | "paper_lookup" | "code_search"（仅 research 有意义，chat 时填 "survey"）。'
        "survey = 广度调研多角度；paper_lookup = 针对单篇论文/单一目标；code_search = 找开源代码/GitHub 实现。\n"
        '  "target": 可直接用于检索的查询词（research 时填，尽量具体；chat 时填空串）。\n'
        '  "carry_context": true/false，这一轮是否延续上文（需要带上轮报告作背景）。话题切换则 false。\n'
        '示例：{"route": "research", "mode": "code_search", "target": "Attention Is All You Need implementation", "carry_context": true}'
    )
    user = (
        f"已有研究背景：\n{reports_block}\n\n"
        f"最近对话：\n{turns_block}\n\n"
        f"用户新输入：{user_input}\n\n只输出 JSON。"
    )
    return [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)]


def _parse(text: str, fallback_target: str) -> IntentResult:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return _degraded(fallback_target)
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, TypeError):
        return _degraded(fallback_target)

    route = data.get("route")
    if route not in _ROUTES:
        return _degraded(fallback_target)

    mode = data.get("mode")
    if mode not in _MODES:
        mode = ResearchMode.SURVEY.value

    target = str(data.get("target") or "").strip() or fallback_target
    carry_context = bool(data.get("carry_context", False))
    return IntentResult(route=route, mode=mode, target=target, carry_context=carry_context)
