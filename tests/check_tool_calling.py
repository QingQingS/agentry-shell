"""
Step B 验证：DeepSeek 工具路径端到端闭环（需 .env 里的 DEEPSEEK_API_KEY）。

给一个假 add(a,b) 工具，问「2+3 等于几」：
  1. 确认 LLM 发出 tool_call（解析出 name/arguments）
  2. 本地算出结果，包成 tool 结果消息回填
  3. 再调一次，确认 LLM 用结果答出 5

跑法：
  PY=/usr/local/Caskroom/miniforge/base/envs/claude-deepseek/bin/python
  $PY tests/check_tool_calling.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Config
from core.llm.base import ChatMessage, ToolSpec
from core.llm.factory import get_llm

ADD_TOOL = ToolSpec(
    name="add",
    description="计算两个整数的和",
    parameters={
        "type": "object",
        "properties": {
            "a": {"type": "integer", "description": "第一个加数"},
            "b": {"type": "integer", "description": "第二个加数"},
        },
        "required": ["a", "b"],
    },
)


async def main() -> int:
    config = Config.from_env()
    llm = get_llm("fast", config)
    print(f"provider={llm.provider_name} model={llm.model}")

    messages = [
        ChatMessage(role="user", content="用 add 工具算一下 2 加 3 等于几？只要最终数字。"),
    ]

    # 第一轮：期望 LLM 发起 tool_call
    resp1 = await llm.chat(messages, tools=[ADD_TOOL])
    print(f"\n[轮1] stop_reason={resp1.stop_reason} tool_calls={resp1.tool_calls}")
    assert resp1.stop_reason == "tool_calls", f"期望发起工具调用，实际 {resp1.stop_reason}"
    assert len(resp1.tool_calls) >= 1, "未解析出任何 tool_call"
    call = resp1.tool_calls[0]
    assert call.name == "add", f"工具名应为 add，实际 {call.name}"
    assert isinstance(call.arguments, dict), "arguments 应解析为 dict"
    a, b = call.arguments.get("a"), call.arguments.get("b")
    assert {a, b} == {2, 3}, f"参数应为 2 和 3，实际 a={a} b={b}"
    print(f"[轮1] ✓ 工具调用解析正确：add(a={a}, b={b}) id={call.id}")

    # 本地执行工具
    result = str(a + b)

    # 回填：assistant 工具调用轮 + tool 结果轮
    messages.append(
        ChatMessage(
            role="assistant",
            content=resp1.content,
            tool_calls=resp1.tool_calls,
            reasoning_content=resp1.reasoning_content,
        )
    )
    messages.append(
        ChatMessage(role="tool", content=result, tool_call_id=call.id)
    )

    # 第二轮：期望 LLM 用结果作答
    resp2 = await llm.chat(messages, tools=[ADD_TOOL])
    print(f"\n[轮2] stop_reason={resp2.stop_reason} content={resp2.content!r}")
    assert resp2.stop_reason == "stop", f"期望正常结束，实际 {resp2.stop_reason}"
    assert "5" in resp2.content, f"回答里应含 5，实际 {resp2.content!r}"
    print("[轮2] ✓ LLM 用工具结果答出 5")

    print(f"\n累计用量：{llm.cumulative_usage.to_dict()}")
    print("\n✅ Step B 工具路径端到端闭环通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
