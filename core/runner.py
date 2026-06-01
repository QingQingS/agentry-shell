"""
统一的 Agent 执行包装。

把"生命周期钩子 + status/error 事件"从各 Agent 和各入口收敛到这一处：
Agent 只负责 yield 领域事件（log / tokens / result / custom）并在失败时抛异常；
何时 running / done / error、何时调 on_start / on_finish / on_error，全部由此保证。

入口层（CLI / REST / WebSocket）统一消费 run_agent(agent, task)，不再直接 agent.run()。
这样新写 Agent 不会因为"忘记调钩子 / 忘记 emit status"而让状态停在 IDLE。
"""

from __future__ import annotations

from typing import AsyncIterator

from core import trace
from core.agent_interface import AgentEvent, AgentInterface


async def run_agent(
    agent: AgentInterface, task: str, **kwargs
) -> AsyncIterator[AgentEvent]:
    # 每次 agent 运行 = 一个 trace run 作用域（步 2）。这是 hub 入口与 dispatch
    # 派发 spoke 的共同咽喉，故 hub→spoke 的父子层级在此统一成立；该 run 内所有
    # LLM 调用（含 spoke 内部工具的子调用）自动归属本 run。
    token = trace.enter_run(getattr(agent, "name", type(agent).__name__))
    try:
        await agent.on_start(task)
        yield AgentEvent(type="status", content="running")

        try:
            async for event in agent.run(task, **kwargs):
                yield event
        except Exception as e:
            await agent.on_error(e)
            yield AgentEvent(type="error", content=f"{type(e).__name__}: {e}")
            yield AgentEvent(type="status", content="error")
            return

        await agent.on_finish()
        yield AgentEvent(type="status", content="done")
    finally:
        trace.exit_run(token)
