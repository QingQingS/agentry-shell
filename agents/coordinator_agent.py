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
from pathlib import Path
from typing import AsyncIterator, List

from core.agent_interface import AgentEvent, AgentInterface
from core.dispatch import DispatchAgentTool #tool
from core.llm import ChatMessage, get_llm
from core.registry import AgentRegistry, build_default_registry
from core.staging import ImportFilesTool #tool
from core.tools import ReadFileTool, ToolRegistry, WikiSearchTool #tool

MAX_ROUNDS = 10          # Coordinator 派发轮上限（防递归式无限分解）
DEFAULT_WIKI_ROOT = "wiki"   # 只读 wiki 检索的根（与 WikiAgent 落点一致）

# spoke 内部事件里，向用户冒泡（扇入）的类型；status/result/error 不冒泡
# （status 是噪音；result 已作 observation，避免 dump；error 由 observation 承载）。
_FORWARD_EVENT_TYPES = ("log", "tokens", "stream", "custom")

SCHEMA_TEMPLATE = """你是一个任务编排中枢（Coordinator）。你把用户的请求分解，按需派发给下列专职 agent 执行，再把结果综合成给用户的最终答复。

可用的 agent：
{catalog}

可用的工具：

- dispatch_agent(agent, prompt, context, files): 把子任务派给一个 spoke。
  - agent：上面清单里的名字。
  - prompt：给该 agent 的自然语言子任务，必须自包含、指代已消解（agent 看不到本对话历史）。
  - context：可选背景，用于把上游 agent 的结果蒸馏后传给下游；无则留空。
  - files：可选，要交给该 agent 处理的工作区文件路径（reports/... 或 uploads/...）。
    归档文件时，把文件路径放进 files，不要塞进 prompt 散文里（prompt 只写归档意图）。
  - 返回 observation 含 status / summary / artifact 行（如果 spoke 落盘了产物，路径写在这里）/ report 段。

- import_files(paths): 把用户提到的外部文件复制到 uploads/。
  paths 是用户给的外部路径列表（绝对或 ~ 路径）。返回每个文件入库后的工作区路径。
  **下游 spoke 永远不直接读外部 path**——任何外部文件必须先经 import_files 进入工作区。

- wiki_search(query): 在已归档的本地 wiki 里按关键词检索已有页面（只读）。
  返回命中页面的 path/标题/片段。回答问题前可先查 wiki 复用已沉淀的知识；
  只命中已策展页面（不含 staging/）。
- read_file(path): 读取某个 wiki 页面的完整内容（path 相对 wiki 根，如 AI/rag.md）。
  配合 wiki_search 使用：先搜到 path，再读全文，然后在答复里引用。

跨 agent 数据流的标准模式：

- 调研任务：dispatch_agent(researcher, ...) → 拿到 artifact: reports/xxx.md（researcher 强制落盘）。
- 归档 researcher 的产出：dispatch_agent(wiki_curator, prompt="把这篇归档进 wiki，按主题归类",
  files=["reports/xxx.md"])。文件搬运由系统在派发时完成，你不必（也无法）自己搬。
- 归档用户提供的外部文件：import_files([...]) → dispatch_agent(wiki_curator,
  prompt="把这些归档进 wiki", files=["uploads/xxx.md"])。
- 简单追问能直接答 → 不必派发。

分解原则：
- 子任务之间无依赖 → 可在一轮里发出多个 dispatch_agent（并行）。
- 有依赖（下游 prompt 需要上游 artifact 路径）→ 先发上游，拿到 artifact 再发下游。

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
        import_tool = ImportFilesTool()
        # 只读 wiki 检索：闭合 research→curate→reuse 的 reuse 一环（命中已归档页面并引用）。
        wiki_root = Path(kwargs.get("wiki_root") or DEFAULT_WIKI_ROOT)
        wiki_search = WikiSearchTool(wiki_root.resolve())
        wiki_read = ReadFileTool(wiki_root.resolve())
        tools = ToolRegistry([dispatch, import_tool, wiki_search, wiki_read])
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

            # 并行扇入：本轮所有 tool_call 并发驱动。
            # 两类调用混排：dispatch_agent → spoke 派发（事件流扇入）；
            #             import_files / wiki_search / read_file → 本地工具，直接 execute。
            indexed = []
            for idx, call in enumerate(resp.tool_calls):
                if call.name == "dispatch_agent":
                    kind = "dispatch"
                    label = call.arguments.get("agent", "?")
                    action_text = f"dispatch_agent({label})"
                else:
                    kind = "local"
                    label = call.name
                    paths_count = len(call.arguments.get("paths", []) or [])
                    action_text = f"{call.name}({paths_count} paths)" if paths_count else call.name
                spoke_id = f"{label}#{idx}"
                indexed.append((call, kind, label, spoke_id))
                yield AgentEvent(
                    type="log",
                    content=action_text,
                    metadata={"trace": "action", "spoke": label, "spoke_id": spoke_id},
                )

            queue: asyncio.Queue = asyncio.Queue()
            done = object()

            async def run_one(call, kind, label, spoke_id):
                if kind == "dispatch":
                    async def on_event(ev):
                        if ev.type in _FORWARD_EVENT_TYPES:
                            await queue.put((spoke_id, label, ev))
                    try:
                        return await dispatch.dispatch(
                            label,
                            call.arguments.get("prompt", ""),
                            call.arguments.get("context", ""),
                            files=call.arguments.get("files"),
                            on_event=on_event,
                        )
                    except Exception as e:  # spoke 构造/驱动意外也不打断循环
                        return f"Error: 派发 {label} 失败: {type(e).__name__}: {e}"
                # 本地工具（import_files / wiki_search / read_file / 未知）走 ToolRegistry，
                # 任何异常已被 ToolRegistry.execute 兜成 observation。
                return await tools.execute(call)

            async def drive():
                obs = await asyncio.gather(*(run_one(c, k, l, s) for c, k, l, s in indexed))
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

            for (call, kind, label, spoke_id), obs in zip(indexed, observations):
                if kind == "dispatch" and not obs.startswith("Error:"):
                    spokes_used.append(label)
                messages.append(ChatMessage(role="tool", content=obs, tool_call_id=call.id))
                yield AgentEvent(
                    type="log",
                    content=obs.splitlines()[0] if obs else "(空)",
                    metadata={"trace": "leaf", "spoke": label, "spoke_id": spoke_id},
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
