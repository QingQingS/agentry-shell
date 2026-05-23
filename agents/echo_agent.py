"""
EchoAgent —— 用于验证基础设施的最简 Agent。

功能：接收任务，逐步模拟处理过程，最后返回结果。
不依赖任何外部服务，可以在没有 API Key 的情况下完整跑通整个框架。

替换为真实 Agent：
    1. 新建 agents/my_agent.py，继承 AgentInterface
    2. 实现 run() 异步生成器
    3. 在 .env 设置 AGENT_CLASS=agents.my_agent.MyAgent
"""

import asyncio
from typing import AsyncIterator

from core.agent_interface import AgentInterface, AgentEvent


class EchoAgent(AgentInterface):
    """
    演示用 Agent，逐步 echo 任务内容，模拟真实 Agent 的流式输出。
    """

    name = "EchoAgent"
    description = "演示用 Agent，验证基础设施连通性。"

    async def run(self, task: str, **kwargs) -> AsyncIterator[AgentEvent]:
        # 生命周期（on_start/on_finish、status running/done）由 core.runner 统一负责。
        yield AgentEvent(type="log", content=f"收到任务：{task}")
        await asyncio.sleep(0.5)

        # ── 阶段 2：模拟处理步骤 ──────────────────────────
        steps = ["分析任务", "收集信息", "整理结果"]
        for i, step in enumerate(steps, 1):
            yield AgentEvent(
                type="log",
                content=f"[{i}/{len(steps)}] {step}...",
                metadata={"step": i, "total": len(steps)},
            )
            await asyncio.sleep(0.8)   # 模拟耗时操作

        # ── 阶段 3：输出结果 ──────────────────────────────
        result = (
            f"## EchoAgent 结果\n\n"
            f"**任务**：{task}\n\n"
            f"这是 EchoAgent 的演示输出。\n"
            f"基础设施（FastAPI / WebSocket / CLI）已全部连通。\n\n"
            f"将此 Agent 替换为真实实现：\n"
            f"1. 继承 `AgentInterface`\n"
            f"2. 实现 `run()` 异步生成器\n"
            f"3. 在 `.env` 设置 `AGENT_CLASS`\n"
        )
        yield AgentEvent(type="result", content=result)
