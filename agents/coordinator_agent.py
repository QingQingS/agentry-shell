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
from core.scope import Scope
from core.tools import ReadFileTool, ToolRegistry, WikiSearchTool #tool

MAX_ROUNDS = 10          # Coordinator 派发轮上限（防递归式无限分解）
STOP_LOSS_THRESHOLD = 2  # 某 spoke 连续多少次未拿到有效结果 → 在仪表盘亮止损提示
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
  返回命中页面的 path（相对项目根，如 wiki/AI/rag.md）/标题/片段。回答问题前可先查 wiki
  复用已沉淀的知识；只命中已策展页面（不含 staging/）。
- read_file(path): 读取工作区内某个文件的完整内容（只读，path 相对项目根，如 reports/xxx.md）。
  可读 researcher 落盘的 reports/，也可读 wiki_search 命中的页面（把它返回的 path 直接传入即可）。

跨 agent 数据流的标准模式：

- 调研任务：dispatch_agent(researcher, ...) → 拿到 artifact: reports/xxx.md（researcher 强制落盘）。
  纯调研、不归档时，可 read_file("reports/xxx.md") 读全文综合给用户。
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
        # Coordinator 的文件权限：read_file 看整个项目（只读），既能 reuse 已归档 wiki 页面，
        # 也能直接读 researcher 落盘的 reports/xxx.md 做综合（纯调研、不归档时尤其需要）。
        # wiki_search 的检索/读取边界仍只在 wiki（不被放开）。
        project_root = Path(kwargs.get("project_root") or ".").resolve()
        wiki_root = Path(kwargs.get("wiki_root") or DEFAULT_WIKI_ROOT)
        if not wiki_root.is_absolute():
            wiki_root = project_root / wiki_root
        wiki_root = wiki_root.resolve()
        # wiki_search 命中 path 原是 wiki 根相对；补成项目相对（如 wiki/AI/rag.md），
        # 与 read_file 的项目根口径一致，reuse→read_file 才接得上。wiki 不在项目内则退回空前缀。
        try:
            wiki_prefix = wiki_root.relative_to(project_root).as_posix() + "/"
        except ValueError:
            wiki_prefix = ""
        wiki_search = WikiSearchTool(Scope(root=wiki_root), path_prefix=wiki_prefix)
        wiki_read = ReadFileTool(
            Scope(root=project_root),
            description="读取工作区内某个文件的完整内容（只读）。path 相对项目根，如 reports/xxx.md。",
        )
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
        # 派发台账：agent 名 → {total, ok, bad, streak(连续未成)}；喂给仪表盘的止损判断。
        ledger: dict = {}
        final_content = ""
        stopped_naturally = False

        for rnd in range(MAX_ROUNDS):
            t0 = time.monotonic()
            # 健康度仪表盘：逐轮变化的内容（预算余量…）必须放在**请求末尾**且**不写进
            # messages**——放开头/改 system 会把 KV cache 分歧点推到 token 0，整段 prompt
            # 每轮全 miss。末尾本就是未缓存的新内容区，搭车免费；不入历史则避免旧仪表盘累积。
            resp = await llm.chat(messages + [self._budget_header(task, rnd, ledger)], tools=specs)
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
                if kind == "dispatch":
                    self._ledger_record(ledger, label, self._obs_status(obs))
                    if not obs.startswith("Error:"):
                        spokes_used.append(label)
                messages.append(ChatMessage(role="tool", content=obs, tool_call_id=call.id))
                yield AgentEvent(
                    type="log",
                    content=obs.splitlines()[0] if obs else "(空)",
                    metadata={"trace": "leaf", "spoke": label, "spoke_id": spoke_id},
                )

        status = "ok"
        if not stopped_naturally:
            # 触顶不再丢占位话术：强制一次「无工具」的诚实收尾，基于整段过程如实交代
            # 做到哪/缺什么/为什么缺/试过哪些/下一步——与 spoke 的 incomplete 契约同构。
            yield AgentEvent(type="log", content=f"达到派发轮上限（{MAX_ROUNDS}），强制诚实收尾。")
            final_content = await self._honest_finish(llm, messages)
            status = "incomplete"

        # tokens 事件后置到收尾调用之后，使累计用量含这次诚实收尾的开销。
        usage = llm.cumulative_usage
        yield AgentEvent(
            type="tokens",
            content=f"input={usage.input_tokens}  output={usage.output_tokens}  total={usage.total_tokens}",
            metadata={**usage.to_dict(), "provider": llm.provider_name, "model": llm.model},
        )

        yield AgentEvent(
            type="result",
            content=final_content.strip(),
            metadata={"spokes_used": spokes_used, "status": status},
        )

    HEALTH_TAG = "[任务健康度·实时]"

    def _budget_header(self, task: str, rnd: int, ledger: dict) -> ChatMessage:
        """逐轮刷新的「健康度仪表盘」——临时消息，拼在请求末尾、不入持久 messages。

        放四块：原始目标（锚定）+ 轮次预算（步2）+ 派发台账与止损（步3）+ 进度自检（步4）。
        台账/止损读自 ledger（截至上一轮的派发结果），让 hub 在决定本轮动作前，
        看见「某条路已连续 N 次没结果」这个事实——该换思路或收尾，而不是闷头重试。
        进度自检放在末尾（最贴近下一步决策），逐轮提醒 hub 对照原始目标，防止因不满意
        上一个结果而不断派生更小更偏的子问题、一路漂离主线。
        used = 已完成轮数；left 含当前轮（rnd=0 时 left=MAX_ROUNDS）。
        """
        used, left = rnd, MAX_ROUNDS - rnd
        lines = [
            self.HEALTH_TAG,
            f"原始目标：{task}",
            f"轮次预算：共 {MAX_ROUNDS} 轮，已用 {used}，剩余 {left}。",
            "提醒：若用户要求里含后续环节（如归档/汇总），务必在剩余轮次里为它留出余地，"
            "别把预算全用在前置调研上；预算见底时优先收尾交付，而不是再开新派发。",
        ]
        if ledger:
            lines.append("派发台账：")
            for name, r in ledger.items():
                lines.append(f"  - {name}：{r['total']} 次（成 {r['ok']} / 未成 {r['bad']}）")
            for name, r in ledger.items():
                if r["streak"] >= STOP_LOSS_THRESHOLD:
                    lines.append(
                        f"⚠️ 止损：{name} 已连续 {r['streak']} 次未拿到有效结果，"
                        "别再用同样方式重试——换思路（换问法/信息源/拆小问题），或停下来如实收尾。"
                    )
        lines.append(
            "进度自检：派下一个子任务前，对照上面的「原始目标」自问——这一步是否仍直接服务于它？"
            "若你只是因对上一个结果不满意而不断派生更小、更偏的子问题，很可能已偏离主线："
            "回到原始目标，或如实收尾，别越钻越细。"
        )
        return ChatMessage(role="user", content="\n".join(lines))

    @staticmethod
    def _obs_status(obs: str) -> str:
        """从一条 dispatch observation 解析 spoke 成色。

        两种形态：`Error: …`（未知 agent / 派发异常）→ error；
        `[<agent>] status=<s>\\n…`（正常 _format 输出）→ 取 <s>（ok/degenerate/incomplete/error）。
        """
        if obs.startswith("Error:"):
            return "error"
        first = obs.splitlines()[0] if obs else ""
        if "status=" in first:
            return first.split("status=", 1)[1].split()[0].strip() or "ok"
        return "ok"

    @staticmethod
    def _ledger_record(ledger: dict, agent: str, status: str) -> None:
        """记一笔派发结果；status=ok 清零连续未成 streak，否则 +1。"""
        rec = ledger.setdefault(agent, {"total": 0, "ok": 0, "bad": 0, "streak": 0})
        rec["total"] += 1
        if status == "ok":
            rec["ok"] += 1
            rec["streak"] = 0
        else:
            rec["bad"] += 1
            rec["streak"] += 1

    FINISH_INSTRUCTION = (
        "你已达到派发轮次上限，从现在起不能再调用任何工具或派发 agent。"
        "请基于以上完整过程，给用户写一份诚实的收尾，不要用占位话术、不要编造未发生的结果：\n"
        "1) 已完成什么——哪些子任务派出去了、各自拿回了什么实质结果（有 artifact 写明路径）。\n"
        "2) 还缺什么——用户最初的要求里，哪一部分还没达成。\n"
        "3) 为什么缺——卡在哪（如检索源离线/查无结果、轮次预算耗尽、某条路走不通）。\n"
        "4) 试过哪些途径——避免接手者重复踩坑。\n"
        "5) 下一步建议——若要继续，往哪个方向走最可能有进展。\n"
        "用面向用户的 Markdown 正文，可长可短，但要让用户一眼看清「做到哪、缺什么、为什么」。"
    )

    async def _honest_finish(self, llm, messages: List[ChatMessage]) -> str:
        """触顶后的诚实收尾：一次无工具 LLM 调用，强制据实交代进度与缺口。

        无工具（tools=None）保证模型只能产出文本、无法再派发。收尾调用本身若失败，
        回退到一句明确点出「触顶 + 看派发日志」的说明，绝不吞掉已有进展。
        """
        probe = messages + [ChatMessage(role="user", content=self.FINISH_INSTRUCTION)]
        try:
            resp = await llm.chat(probe)
            text = (resp.content or "").strip()
            if text:
                return text
        except Exception:  # noqa: BLE001 —— 收尾调用失败不该再掀翻整个 run
            pass
        return (
            f"（已达派发轮上限（{MAX_ROUNDS}）提前结束，且自动收尾未能生成说明。"
            "本次任务未完成，请查看上方各轮派发日志了解已派发的部分。）"
        )
