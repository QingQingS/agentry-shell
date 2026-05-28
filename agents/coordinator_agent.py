"""
CoordinatorAgent —— v2 hub-and-spoke 的中枢（替代 v1 的 intent 路由 OrchestratorAgent）。

它本身是一个 ReAct 循环，工具表里只有一个特殊工具 `dispatch_agent`：
  - 把用户任务临场分解，按需把子任务派给注册表里的 spoke（researcher / wiki_curator…）；
  - 依赖不做显式 DAG，而靠循环涌现：无依赖→一轮多 tool_call（未来并行）；
    有依赖→分轮串行（拿到上游 observation 再写下游 prompt）。
  - 不再 if/elif 硬编码路由——「派给谁」是 LLM 读注册表 catalog 后的决策。

退化谱（与 v1 路由的关系）：
    0 个 dispatch → chat（直接用已有上下文作答，吸收了原 ChatAgent）
    1 个 dispatch → 退化路由（= 旧单 worker 路由）
    N 个 dispatch → 动态分解

结构复用 WikiAgent 循环：MAX_ROUNDS 兜底、reasoning_content 回传、错误转 observation
（由 dispatch 工具/ToolRegistry 边界保证，循环永不被工具异常打断）、trace 日志。

收尾契约（决策 B + 8.1）：最终 `result` 事件 content = 给用户的 markdown，
spokes_used 走 metadata —— 用户不该看到裸 JSON。
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, List

from core.agent_interface import AgentEvent, AgentInterface
from core.dispatch import DispatchAgentTool
from core.llm import ChatMessage, get_llm
from core.registry import AgentRegistry, build_default_registry
from core.tools import ToolRegistry

MAX_ROUNDS = 10          # Coordinator 派发轮上限（防递归式无限分解）

# spoke 内部事件里，向用户冒泡（扇入）的类型；status/result/error 不冒泡
# （status 是噪音；result 已作 observation，避免 dump；error 由 observation 承载）。
_FORWARD_EVENT_TYPES = ("log", "tokens", "stream", "custom")

SCHEMA_TEMPLATE = """你是一个任务编排中枢（Coordinator）。你把用户的请求分解，按需派发给下列专职 agent 执行，再把结果综合成给用户的最终答复。

可用的 agent：
{catalog}

派发方式——调用 dispatch_agent(agent, prompt, context)：
- agent：上面清单里的名字。
- prompt：给该 agent 的自然语言子任务，必须自包含、指代已消解（agent 看不到本对话历史）。
- context：可选背景，用于把上游 agent 的结果蒸馏后传给下游；无则留空。

分解原则：
- 子任务之间无依赖 → 可在一轮里发出多个 dispatch_agent（并行）。
- 有依赖（下游 prompt 需要上游结果）→ 先发上游，拿到返回后再发下游。
- 能直接用已有信息回答的简单追问 → 不必派发，直接作答。

结束：当你拿到足够信息后，**不要再调用工具**，直接输出给用户的最终答复。
最终答复用 Markdown 正文，面向用户，不要输出 JSON、不要复述工具调用细节。
"""


class CoordinatorAgent(AgentInterface):
    name = "CoordinatorAgent"
    description = "任务编排中枢：分解任务 → 派发 spoke → 综合答复（hub-and-spoke）。"

    async def run(self, task: str, **kwargs) -> AsyncIterator[AgentEvent]:
        # 生命周期/异常→error 由 core.runner 统一负责；这里只 yield 领域事件、失败时抛异常。
        registry: AgentRegistry = kwargs.get("registry") or build_default_registry()
        dispatch = DispatchAgentTool(registry, config=self.config, websocket=self.websocket)
        tools = ToolRegistry([dispatch])
        specs = tools.specs()

        llm = get_llm(tier="smart", config=self.config)
        yield AgentEvent(
            type="log",
            content=f"使用 {llm.provider_name} / {llm.model}；可用 agent：{', '.join(registry.names())}",
            metadata={"provider": llm.provider_name, "model": llm.model},
        )

        messages = [
            ChatMessage(role="system", content=SCHEMA_TEMPLATE.format(catalog=registry.catalog())),
            ChatMessage(role="user", content=task),
        ]

        spokes_used: List[str] = []
        final_content = ""
        stopped_naturally = False

        for rnd in range(MAX_ROUNDS):
            t0 = time.monotonic()
            resp = await llm.chat(messages, tools=specs)
            dt = time.monotonic() - t0
            messages.append(
                ChatMessage(
                    role="assistant",
                    content=resp.content,
                    tool_calls=resp.tool_calls,
                    reasoning_content=resp.reasoning_content,  # 思考模型回传约束
                )
            )

            think = (resp.reasoning_content or resp.content or "").strip()
            if think:
                yield AgentEvent(type="log", content=think, metadata={"trace": "think"})
            yield AgentEvent(
                type="log",
                content=f"第 {rnd + 1} 轮 · {dt:.1f}s · +{resp.usage.total_tokens} tokens",
                metadata={"trace": "leaf"},
            )

            if not resp.tool_calls:
                final_content = resp.content
                stopped_naturally = True
                break

            # 并行扇入：本轮所有 tool_call 并发驱动，各 spoke 事件合并成一条流（带 spoke_id）。
            indexed = []
            for idx, call in enumerate(resp.tool_calls):
                agent_name = call.arguments.get("agent", "?")
                spoke_id = f"{agent_name}#{idx}"
                indexed.append((call, agent_name, spoke_id))
                yield AgentEvent(
                    type="log",
                    content=f"dispatch_agent({agent_name})",
                    metadata={"trace": "action", "spoke": agent_name, "spoke_id": spoke_id},
                )

            queue: asyncio.Queue = asyncio.Queue()
            done = object()

            async def run_one(call, agent_name, spoke_id):
                if call.name != "dispatch_agent":
                    return f"Error: 未知工具: {call.name}"

                async def on_event(ev):
                    if ev.type in _FORWARD_EVENT_TYPES:
                        await queue.put((spoke_id, agent_name, ev))

                try:
                    return await dispatch.dispatch(
                        agent_name,
                        call.arguments.get("prompt", ""),
                        call.arguments.get("context", ""),
                        on_event=on_event,
                    )
                except Exception as e:  # spoke 构造/驱动意外也不打断循环
                    return f"Error: 派发 {agent_name} 失败: {type(e).__name__}: {e}"

            async def drive():
                obs = await asyncio.gather(*(run_one(c, a, s) for c, a, s in indexed))
                await queue.put(done)
                return obs

            driver = asyncio.create_task(drive())
            while True:
                item = await queue.get()
                if item is done:
                    break
                spoke_id, agent_name, ev = item
                yield AgentEvent(
                    type=ev.type,
                    content=ev.content,
                    metadata={**ev.metadata, "spoke": agent_name, "spoke_id": spoke_id},
                )
            observations = await driver

            for (call, agent_name, spoke_id), obs in zip(indexed, observations):
                if not obs.startswith("Error:"):
                    spokes_used.append(agent_name)
                messages.append(ChatMessage(role="tool", content=obs, tool_call_id=call.id))
                yield AgentEvent(
                    type="log",
                    content=obs.splitlines()[0] if obs else "(空)",
                    metadata={"trace": "leaf", "spoke": agent_name, "spoke_id": spoke_id},
                )

        usage = llm.cumulative_usage
        yield AgentEvent(
            type="tokens",
            content=f"input={usage.input_tokens}  output={usage.output_tokens}  total={usage.total_tokens}",
            metadata={**usage.to_dict(), "provider": llm.provider_name, "model": llm.model},
        )

        if not stopped_naturally:
            yield AgentEvent(type="log", content=f"达到派发轮上限（{MAX_ROUNDS}），提前结束。")
            final_content = final_content or "（因达派发轮上限提前结束，结果可能不完整。）"

        yield AgentEvent(
            type="result",
            content=final_content.strip(),
            metadata={"spokes_used": spokes_used},
        )
