"""
WikiAgent —— 项目首个真正 agentic 的 Agent（ReAct 工具循环）。

接收一份或多份 .md 文档，由 LLM 自主决定读哪些 wiki 页、写哪些页、怎么更新
index.md，把知识整合进以主题为中心的本地知识库。

设计存档见 wiki-agent开发.md 第八（工具层）/九（SCHEMA）/十（循环兜底）节。
关键约束：
  - SCHEMA 进 system prompt；工具来自 core/tools.py 的沙箱注册表
  - assistant 工具调用轮必须回传 reasoning_content（思考模型要求，否则 400）
  - 工具永不抛异常（返回 observation 字符串）；循环只在「无 tool_call」或触顶 MAX_STEPS 时停
  - 兜圈子：同一 (name,args) 重复 ≥ DUP_THRESHOLD 次时注入一次 nudge
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import AsyncIterator, List, Tuple

from core.agent_interface import AgentEvent, AgentInterface
from core.llm import ChatMessage, get_llm
from core.tools import build_wiki_registry

from .wiki_schema import WIKI_SCHEMA

MAX_STEPS = 20          # 单次 ingest 的 LLM 调用上限
DUP_THRESHOLD = 3       # 同一调用重复多少次触发 nudge
DEFAULT_WIKI_ROOT = "wiki"

NUDGE_TEXT = (
    "提醒：你已多次执行同一个工具调用并得到相同结果，似乎卡住了。"
    "请改变策略——换个路径/参数，或者如果工作已完成，直接用文字总结并结束（不要再调用工具）。"
)


class WikiAgent(AgentInterface):
    name = "WikiAgent"
    description = "知识策展：把 .md 文档整合进以主题为中心的本地 wiki（agentic 工具循环）。"

    async def run(self, task: str, **kwargs) -> AsyncIterator[AgentEvent]:
        # 生命周期/异常→error 由 core.runner 统一负责；这里只 yield 领域事件、失败时抛异常。
        files = self._resolve_input_paths(task, kwargs)
        docs: List[Tuple[str, str]] = []
        for p in files:
            if p.is_file():
                docs.append((p.name, p.read_text(encoding="utf-8")))
                yield AgentEvent(type="log", content=f"读取输入文档：{p.name}")
            else:
                yield AgentEvent(type="log", content=f"输入文档不存在，跳过：{p}")
        if not docs:
            raise ValueError("没有可读的输入 .md 文档")

        wiki_root = Path(kwargs.get("wiki_root") or DEFAULT_WIKI_ROOT)
        registry = build_wiki_registry(wiki_root)
        specs = registry.specs()
        llm = get_llm(tier="smart", config=self.config)
        yield AgentEvent(
            type="log",
            content=f"使用 {llm.provider_name} / {llm.model}；wiki 根目录：{wiki_root.resolve()}",
            metadata={"provider": llm.provider_name, "model": llm.model},
        )

        today = date.today().isoformat()
        messages = [
            ChatMessage(role="system", content=WIKI_SCHEMA),
            ChatMessage(role="user", content=self._format_input(docs, today)),
        ]

        touched: List[str] = []          # 本次成功写入/更新的页面（去重前）
        call_counts: dict[tuple, int] = {}
        nudged = False
        final_content = ""
        stopped_naturally = False

        for _step in range(MAX_STEPS):
            resp = await llm.chat(messages, tools=specs)
            messages.append(
                ChatMessage(
                    role="assistant",
                    content=resp.content,
                    tool_calls=resp.tool_calls,
                    reasoning_content=resp.reasoning_content,  # 思考模型回传约束
                )
            )
            if resp.content.strip():
                yield AgentEvent(type="log", content=f"思考：{resp.content.strip()}")

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
                    content=self._describe_call(call.name, call.arguments, obs),
                )
                if call.name == "write_file" and not obs.startswith("Error:"):
                    path = call.arguments.get("path")
                    if path:
                        touched.append(path)

            if not nudged and any(c >= DUP_THRESHOLD for c in call_counts.values()):
                messages.append(ChatMessage(role="user", content=NUDGE_TEXT))
                yield AgentEvent(type="log", content="检测到重复调用，已注入提醒（nudge）")
                nudged = True

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
    def _resolve_input_paths(task: str, kwargs: dict) -> List[Path]:
        """优先取 kwargs['files']（Orchestrator 传 payload）；否则把 task 当空白分隔的路径。"""
        files = kwargs.get("files")
        if files:
            return [Path(f) for f in files]
        return [Path(tok) for tok in task.split() if tok.strip()]

    @staticmethod
    def _format_input(docs: List[Tuple[str, str]], today: str) -> str:
        parts = [f"今天的日期是 {today}。下面是待归档的 {len(docs)} 份文档，请整合进 wiki：\n"]
        for name, content in docs:
            parts.append(f"\n===== 文档：{name} =====\n{content}\n===== 文档结束：{name} =====")
        return "\n".join(parts)

    @staticmethod
    def _describe_call(name: str, args: dict, obs: str) -> str:
        is_err = obs.startswith("Error:")
        prefix = "✗ " if is_err else ""
        if name == "read_file":
            base = f"{prefix}读取页面 {args.get('path')}"
        elif name == "write_file":
            base = f"{prefix}写入页面 {args.get('path')}"
        elif name == "list_files":
            base = f"{prefix}列出页面" + (f"（{args.get('subdir')}）" if args.get("subdir") else "")
        else:
            base = f"{prefix}调用 {name}"
        return f"{base} → {obs}" if is_err else base

    @staticmethod
    def _summarize(touched: List[str]) -> str:
        uniq = sorted(set(touched))
        if not uniq:
            return "本次未写入任何页面。"
        return f"本次写入/更新 {len(uniq)} 个页面：" + "、".join(uniq) + "。"
