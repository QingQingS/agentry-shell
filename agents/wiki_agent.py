"""
WikiAgent —— 项目首个真正 agentic 的 Agent（ReAct 工具循环）。

接收自然语言归档指令（可携带上游背景），LLM 自主决定读哪些 wiki 页、写哪些页、
是否要 read_source 取外部原文，把知识整合进以主题为中心的本地知识库。

设计存档见 wiki-agent开发.md 第八（工具层）/九（SCHEMA）/十（循环兜底）节。
关键约束：
  - 入口契约：(task, context=...) 与 ResearchAgent/ChatAgent 对齐；
    task 是自然语言指令，content 已在 prompt/context 里就直接归档，只给路径就让 LLM 调 read_source。
  - SCHEMA 进 system prompt；工具来自 core/tools.py 的沙箱注册表
  - assistant 工具调用轮必须回传 reasoning_content（思考模型要求，否则 400）
  - 工具永不抛异常（返回 observation 字符串）；循环只在「无 tool_call」或触顶 MAX_STEPS 时停
  - 兜圈子：同一 (name,args) 重复 ≥ DUP_THRESHOLD 次时注入一次 nudge
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import AsyncIterator, List

from core.agent_interface import AgentEvent, AgentInterface
from core.llm import ChatMessage, get_llm
from core.tools import build_wiki_registry
from core.wiki_index import build_catalog, regenerate_index

from .wiki_schema import WIKI_SCHEMA

MAX_STEPS = 20          # 单次 ingest 的 LLM 调用上限
DUP_THRESHOLD = 3       # 同一调用重复多少次触发 nudge
DEFAULT_WIKI_ROOT = "wiki"

NUDGE_TEXT = (
    "提醒：你已多次执行同一个工具调用并得到相同结果，似乎卡住了。"
    "请改变策略——换个路径/参数，或者如果工作已完成，直接用文字总结并结束（不要再调用工具）。"
    "已经试过多个不同思路、仍拿不到足够信息 —— 也不要再空转或反复重试。"
    "诚实地总结一下你目前的状态：你做到了什么、哪些没完成、为什么没完成"

)


class WikiAgent(AgentInterface):
    name = "WikiAgent"
    description = "知识策展：把 .md 文档整合进以主题为中心的本地 wiki（agentic 工具循环）。"

    async def run(self, task: str, **kwargs) -> AsyncIterator[AgentEvent]:
        # 生命周期/异常→error 由 core.runner 统一负责；这里只 yield 领域事件、失败时抛异常。
        context = (kwargs.get("context") or "").strip()
        # files：派发时由 pre-hook（stage_wiki_inputs）搬进 staging/ 后改写成的 staging 内文件名。
        # 直接结构化拿到，不再从 prompt 散文里正则抠路径（消灭脆弱性）。
        files = [f for f in (kwargs.get("files") or []) if f]
        wiki_root = Path(kwargs.get("wiki_root") or DEFAULT_WIKI_ROOT)
        registry = build_wiki_registry(wiki_root)
        specs = registry.specs()
        llm = get_llm(tier="smart", config=self.config)
        yield AgentEvent(
            type="log",
            content=f"使用 {llm.provider_name} / {llm.model}；wiki 根目录：{wiki_root.resolve()}",
            metadata={"provider": llm.provider_name, "model": llm.model},
        )
        if context:
            yield AgentEvent(type="log", content="（携带上游背景）")

        today = date.today().isoformat()
        catalog = build_catalog(wiki_root)   # 代码侧目录，注入 prompt（LLM 不再读 index.md）
        messages = [
            ChatMessage(role="system", content=WIKI_SCHEMA),
            ChatMessage(
                role="user",
                content=self._format_input(today, catalog, context, task, files),
            ),
        ]

        touched: List[str] = []          # 本次成功写入/更新的页面（去重前）
        call_counts: dict[tuple, int] = {}
        nudged = False
        final_content = ""
        stopped_naturally = False

        for step in range(MAX_STEPS):
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

            # 思考轨迹：思考模型把推理链放在 reasoning_content（content 此时常为空）
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
                # sig = (call.name, json.dumps(call.arguments, sort_keys=True, ensure_ascii=False))
                sig = (call.name,)
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
                if call.name == "write_file" and not obs.startswith("Error:"):
                    path = call.arguments.get("path")
                    if path:
                        touched.append(path)

            if not nudged and any(c >= DUP_THRESHOLD for c in call_counts.values()):
                messages.append(ChatMessage(role="user", content=NUDGE_TEXT))
                yield AgentEvent(type="log", content="检测到重复调用，已注入提醒（nudge）")
                nudged = True

        # 循环结束：index.md 由代码从各页 frontmatter 确定性重生成（LLM 全程不读写它）
        regenerate_index(wiki_root, today)
        yield AgentEvent(type="log", content="index.md 已由系统从各页 frontmatter 重新生成")

        usage = llm.cumulative_usage
        yield AgentEvent(
            type="tokens",
            content=(
                f"input={usage.input_tokens}  output={usage.output_tokens}  "
                f"total={usage.total_tokens}"
            ),
            metadata={**usage.to_dict(), "provider": llm.provider_name, "model": llm.model},
        )

        if stopped_naturally:
            result = final_content.strip() or self._summarize(touched)
        else:
            yield AgentEvent(
                type="log",
                content=f"达到步数上限（{MAX_STEPS}），提前结束。",
            )
            result = self._summarize(touched) + "（因达步数上限提前结束，wiki 可能处于中间状态）"

        yield AgentEvent(type="result", content=result)

    # ---- 辅助 ----

    @staticmethod
    def _format_input(
        today: str, catalog: str, context: str, task: str, files: List[str]
    ) -> str:
        parts = [
            f"今天的日期是 {today}。",
            "\nwiki 当前目录（系统维护，据此了解已有类别与页面；不必读 index.md）：",
            catalog,
        ]
        if context:
            parts.append(
                "\n上游背景（供归档参考；若上游已含完整文档原文，直接基于此归档，不要再调 read_source）："
            )
            parts.append(context)
        if files:
            parts.append(
                "\n待归档的源文档已就位（在 staging/ 内）。逐个用 read_source(path) 读取原文后归档，path 用下面的文件名："
            )
            parts.extend(f"- {name}" for name in files)
        parts.append("\n用户的归档指令：")
        parts.append(task or "（空——先用 list_files 看现状再决定下一步）")
        return "\n".join(parts)

    @staticmethod
    def _describe_action(name: str, args: dict) -> str:
        """工具调用的主行描述，形如 read_file(index.md)。"""
        if name == "read_file":
            return f"read_file({args.get('path')})"
        if name == "write_file":
            return f"write_file({args.get('path')})"
        if name == "list_files":
            sub = args.get("subdir")
            return f"list_files({sub})" if sub else "list_files()"
        if name == "read_source":
            return f"read_source({args.get('path')})"
        return f"{name}({json.dumps(args, ensure_ascii=False)})"

    @staticmethod
    def _summarize_obs(name: str, obs: str) -> str:
        """工具结果的缩进子行摘要（只给摘要，不把全文灌进 log）。"""
        if obs.startswith("Error:"):
            return f"✗ {obs}"
        if name == "read_file":
            return f"读取 {obs.count(chr(10)) + 1} 行 / {len(obs)} 字"
        if name == "read_source":
            return f"读取外部源 {obs.count(chr(10)) + 1} 行 / {len(obs)} 字"
        if name == "list_files":
            if obs.startswith("("):       # "(wiki 内暂无 .md 页面)"
                return obs
            return f"列出 {obs.count(chr(10)) + 1} 个页面"
        return obs                        # write_file 的 obs 已是简短摘要

    @staticmethod
    def _summarize(touched: List[str]) -> str:
        uniq = sorted(set(touched))
        if not uniq:
            return "本次未写入任何页面。"
        return f"本次写入/更新 {len(uniq)} 个页面：" + "、".join(uniq) + "。"
