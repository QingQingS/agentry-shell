"""
dispatch_agent 工具 —— Coordinator 把子任务派给一个 spoke 执行。

它就是 Coordinator ReAct 循环工具表里的那个特殊工具：复用 core/tools.py 的
Tool ABC，因此能直接放进 ToolRegistry，被同一套循环 execute()。

执行语义（hub-and-spoke 的上下文隔离）：
    - 每次派发都从注册表工厂新建一个 **全新** spoke 实例（不共享状态）。
    - 只喂 prompt + context（spoke 自己的 system prompt 由它自己持有）；
      不灌 Coordinator 的对话历史，也看不到兄弟 spoke。
    - spoke 经 core.runner.run_agent 驱动（生命周期/异常统一兜底），其事件流在
      此被排空；只把 **结构化结果摘要** 作为 observation 返回循环——绝不把完整
      transcript 灌回 Coordinator（呼应 WikiAgent −72% 教训）。

返回契约（observation 字符串）：
    [<agent>] status=ok | error
    summary: <一句话>
    artifact: <路径>        # 有才出现
    key_facts: <事实>        # 有才出现

  结果优先取 spoke 的 `result` 事件 metadata（status/summary/artifact_path/
  key_facts）；spoke 尚未产出结构化 metadata 时，从 result 正文合成 fallback
  摘要。run_agent 捕获到的 spoke 异常会以 error 事件出现 → status=error。

  本工具永不向循环抛异常（任何意外由 ToolRegistry.execute 边界兜成 observation）。
"""

from __future__ import annotations

import re
from typing import Awaitable, Callable, Optional

from core.agent_interface import AgentEvent, AgentInterface
from core.llm.base import ToolSpec
from core.registry import AgentRegistry
from core.runner import run_agent
from core.tools import Tool

_SUMMARY_LEN = 200

# 事件回调：spoke 运行时每个事件回调一次（Coordinator 用它扇入 + 打 spoke_id 标签）。
EventSink = Callable[[AgentEvent], Awaitable[None]]


class DispatchAgentTool(Tool):
    spec = ToolSpec(
        name="dispatch_agent",
        description=(
            "把一个子任务派发给一个专职 agent 执行，返回该 agent 的结构化结果摘要。"
            "agent 是注册表中的名字；prompt 是给它的自然语言子任务；"
            "context 是可选背景（上游结果蒸馏，无则留空）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "要派发的 agent 名（见可用 agent 清单）",
                },
                "prompt": {
                    "type": "string",
                    "description": "给该 agent 的自然语言子任务（指代须已消解、自包含）",
                },
                "context": {
                    "type": "string",
                    "description": "可选背景（上游结果蒸馏）；无则空串",
                },
            },
            "required": ["agent", "prompt"],
        },
    )

    def __init__(self, registry: AgentRegistry, config=None, websocket=None):
        self.registry = registry
        self.config = config
        self.websocket = websocket

    async def execute(self, agent: str, prompt: str, context: str = "") -> str:
        """Tool ABC 入口（ToolRegistry 用）：单发、吞掉 spoke 事件，只回 observation。"""
        return await self.dispatch(agent, prompt, context, on_event=None)

    async def dispatch(
        self, agent: str, prompt: str, context: str = "", on_event: Optional[EventSink] = None
    ) -> str:
        """查表 + 隔离驱动一个 spoke。on_event 非空时把 spoke 每个事件回调出去（供扇入）。"""
        spec = self.registry.get(agent)
        if spec is None:
            avail = ", ".join(self.registry.names())
            return f"Error: 未知 agent: {agent}（可用：{avail}）"
        instance = spec.factory(config=self.config, websocket=self.websocket)
        return await self._run_isolated(spec.name, instance, prompt, context, on_event)

    async def _run_isolated(
        self,
        agent_name: str,
        instance: AgentInterface,
        prompt: str,
        context: str,
        on_event: Optional[EventSink] = None,
    ) -> str:
        """隔离驱动 spoke，排空事件流（按需回调），抽出结构化结果摘要。"""
        result_content = ""
        result_meta: dict = {}
        error_text = None

        async for ev in run_agent(instance, prompt, context=context):
            if on_event is not None:
                await on_event(ev)
            if ev.type == "result":
                result_content = ev.content
                result_meta = ev.metadata or {}
            elif ev.type == "error":
                error_text = ev.content

        if error_text is not None:
            return self._format(agent_name, "error", error_text, None, None, report=None)

        status = result_meta.get("status", "ok")
        summary = result_meta.get("summary") or self._snippet(result_content)
        artifact = result_meta.get("artifact_path")
        key_facts = result_meta.get("key_facts")
        return self._format(
            agent_name, status, summary, artifact, key_facts,
            report=result_content or None,
        )

    @staticmethod
    def _snippet(text: str) -> str:
        flat = re.sub(r"\s+", " ", text).strip()
        if not flat:
            return "(无结果输出)"
        return flat[:_SUMMARY_LEN] + ("…" if len(flat) > _SUMMARY_LEN else "")

    @staticmethod
    def _format(agent_name, status, summary, artifact, key_facts, report=None) -> str:
        """observation 字符串：status/summary 必出；artifact/key_facts/report 有才出。

        report = spoke 最终 result.content 原文（完整 markdown）。2026-05-28 决定
        改为带回完整成品而非只摘要——hub 在自己 LLM 上下文里使用，不再需要回读文件。
        """
        lines = [f"[{agent_name}] status={status}", f"summary: {summary}"]
        if artifact:
            lines.append(f"artifact: {artifact}")
        if key_facts:
            lines.append(f"key_facts: {key_facts}")
        if report:
            lines.extend(["---", "report:", report])
        return "\n".join(lines)
