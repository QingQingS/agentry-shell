"""
OrchestratorAgent —— 连续对话编排中枢（阶段三）。

存在意义：worker（ResearchAgent / ChatAgent）保持无状态，每次 run() 失忆；
Orchestrator 把「一串无状态 worker 调用」缝成「连贯对话」，是跨轮状态的家。

每轮 run(task)：
    1. 取 Session 最近上下文
    2. classify_intent（fast LLM）→ {route, mode, target, carry_context, files}
    3. 按 route 路由到 worker，直接调 worker.run()（不过 run_agent，避免嵌套 status），
       carry_context 时把最近一份报告正文作背景注入；转发 worker 的领域事件。
       route=research → ResearchAgent；route=chat → ChatAgent；
       route=wiki → WikiAgent（透传 files，归档 .md 进持久化 wiki）
    4. 写回 Session（research 落报告 + 短文本 turn；chat/wiki 落回答 turn）

约束：
    - _session_manager 是 class-level 单例；session_id = id(self.websocket)
      （WS 每连接唯一；CLI websocket=None → id(None) 全进程恒定 → 多轮天然有效）
    - 不改 WebSocketManager / runner.py，只换 .env 的 AGENT_CLASS
    - 唯一 LLM 用途是意图分类（fast）。报告摘要暂用正文快照（见 CONTEXT §六 待办：
      后期 ResearchAgent 输出结构化 JSON 后，description 由其产出、本类只负责存）
"""

from __future__ import annotations

import re
from typing import AsyncIterator, ClassVar

from agents.chat_agent import ChatAgent
from agents.research_agent import ResearchAgent
from agents.wiki_agent import WikiAgent
from core.agent_interface import AgentEvent, AgentInterface
from core.intent import IntentResult, classify_intent
from core.llm import get_llm
from core.session import RecentContext, SessionManager


class OrchestratorAgent(AgentInterface):
    name = "OrchestratorAgent"
    description = "连续对话编排：意图分类 → 路由 worker → 写回 Session。"

    _session_manager: ClassVar[SessionManager] = SessionManager()

    SNIPPET_LEN = 120

    def _session_id(self) -> str:
        return str(id(self.websocket))

    @classmethod
    def _snippet(cls, text: str) -> str:
        """报告正文的短快照，作 description / 短 turn 文本（零 LLM，后期由 ResearchAgent 产出取代）。"""
        flat = re.sub(r"\s+", " ", text).strip()
        return flat[: cls.SNIPPET_LEN] + ("…" if len(flat) > cls.SNIPPET_LEN else "")

    @staticmethod
    def _recent_report_text(ctx: RecentContext) -> str:
        return ctx.reports[-1].content if ctx.reports else ""

    async def run(self, task: str, **kwargs) -> AsyncIterator[AgentEvent]:
        # 生命周期与异常→error 由 core.runner 统一负责（本类作为 AGENT_CLASS 被 run_agent 驱动）。
        sid = self._session_id()
        ctx = self._session_manager.get_recent_context(sid)
        fast = get_llm(tier="fast", config=self.config)

        intent = await classify_intent(task, ctx, fast)
        yield AgentEvent(
            type="log",
            content=f"意图 route={intent.route} mode={intent.mode} carry={intent.carry_context} target={intent.target!r}",
            metadata={
                "route": intent.route,
                "mode": intent.mode,
                "carry_context": intent.carry_context,
                "target": intent.target,
            },
        )

        background = self._recent_report_text(ctx) if intent.carry_context else ""

        if intent.route == "research":
            worker: AgentInterface = ResearchAgent(config=self.config, websocket=self.websocket)
            run_iter = worker.run(intent.target, mode=intent.mode, background_context=background)
        elif intent.route == "wiki":
            # wiki 是持久化知识库，跨 session 共享 → 不传 session 级 wiki_root，用 WikiAgent 默认
            worker = WikiAgent(config=self.config, websocket=self.websocket)
            run_iter = worker.run(task, files=intent.files)
        else:
            worker = ChatAgent(config=self.config, websocket=self.websocket)
            run_iter = worker.run(task, context=background)

        result_text = ""
        async for ev in run_iter:
            if ev.type == "result":
                result_text = ev.content
            yield ev

        self._write_back(sid, task, intent, result_text)

    def _write_back(self, sid: str, task: str, intent: IntentResult, result_text: str) -> None:
        if intent.route == "research" and result_text:
            description = self._snippet(result_text)
            self._session_manager.save_report(
                sid, topic=intent.target, description=description, content=result_text
            )
            self._session_manager.add_turn(
                sid, task, description, route=intent.route, mode=intent.mode
            )
        else:
            self._session_manager.add_turn(
                sid, task, result_text, route=intent.route, mode=intent.mode
            )
