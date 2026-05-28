"""
v2 Step 1 验证脚本 —— agent 注册表 + dispatch_agent 工具（离线）。

不发网络请求：用零依赖的 EchoAgent 验证派发机制的闭环：
    $PY tests/check_dispatch.py
全部断言通过则打印 OK。

覆盖：
  1. 直接 execute 已知 agent → status=ok + 从裸 result 合成的 summary（fallback 路径）。
  2. execute 未知 agent → "Error: 未知 agent ..."，不抛异常。
  3. 经 core.tools.ToolRegistry 路径派发 → 与直接 execute 一致（证明可放进循环工具表）。
  4. result 事件带结构化 metadata 时优先采用（status/summary/artifact_path/key_facts）。
  5. 默认注册表含两个真 spoke，catalog 渲染出 name/输入/返回 契约。
  6. 上下文隔离：每次派发是全新实例（不共享状态）。
"""

import asyncio
import sys
from pathlib import Path
from typing import AsyncIterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.echo_agent import EchoAgent  # noqa: E402
from core.agent_interface import AgentEvent, AgentInterface  # noqa: E402
from core.dispatch import DispatchAgentTool  # noqa: E402
from core.llm.base import ToolCall  # noqa: E402
from core.registry import AgentRegistry, AgentSpec, build_default_registry  # noqa: E402
from core.tools import ToolRegistry  # noqa: E402


class StructuredAgent(AgentInterface):
    """收尾发结构化 metadata 的 spoke，验证 dispatch 优先采用 metadata。"""

    name = "StructuredAgent"

    async def run(self, task: str, **kwargs) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(
            type="result",
            content="# 人类可读的 markdown 正文",
            metadata={
                "status": "ok",
                "summary": "结构化的一句话结论",
                "artifact_path": "reports/demo.md",
                "key_facts": "事实A；事实B",
            },
        )


class CountingAgent(AgentInterface):
    """构造时自增类计数，用于断言每次派发都是新实例。"""

    instances = 0
    name = "CountingAgent"

    def __init__(self, config=None, websocket=None):
        super().__init__(config=config, websocket=websocket)
        CountingAgent.instances += 1

    async def run(self, task: str, **kwargs) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(type="result", content="ok")


def _echo_registry() -> AgentRegistry:
    return AgentRegistry([
        AgentSpec(
            name="echo",
            description="演示 spoke",
            input_contract="prompt=任意",
            output_contract="回显",
            factory=lambda config=None, websocket=None: EchoAgent(
                config=config, websocket=websocket
            ),
        ),
    ])


async def main() -> None:
    # 1) 已知 agent，裸 result → fallback 合成 summary
    tool = DispatchAgentTool(_echo_registry())
    obs = await tool.execute(agent="echo", prompt="你好", context="")
    assert obs.startswith("[echo] status=ok"), obs
    assert "summary:" in obs, obs
    assert "EchoAgent 结果" in obs or "演示输出" in obs, f"summary 应取自 echo 正文: {obs}"

    # 2) 未知 agent → Error 字符串，不抛
    obs = await tool.execute(agent="nope", prompt="x")
    assert obs.startswith("Error: 未知 agent: nope"), obs
    assert "echo" in obs, "应列出可用 agent"

    # 3) 经 ToolRegistry 路径，结果一致
    reg = ToolRegistry([DispatchAgentTool(_echo_registry())])
    call = ToolCall(id="c1", name="dispatch_agent", arguments={"agent": "echo", "prompt": "hi"})
    obs_via_registry = await reg.execute(call)
    assert obs_via_registry.startswith("[echo] status=ok"), obs_via_registry

    # 4) 结构化 metadata 优先
    sreg = AgentRegistry([
        AgentSpec(
            name="structured", description="d", input_contract="i", output_contract="o",
            factory=lambda config=None, websocket=None: StructuredAgent(),
        ),
    ])
    obs = await DispatchAgentTool(sreg).execute(agent="structured", prompt="go")
    assert "status=ok" in obs
    assert "summary: 结构化的一句话结论" in obs, obs
    assert "artifact: reports/demo.md" in obs, obs
    assert "key_facts: 事实A；事实B" in obs, obs

    # 5) 默认注册表 + catalog 契约
    default = build_default_registry()
    assert set(default.names()) == {"researcher", "wiki_curator"}, default.names()
    cat = default.catalog()
    for token in ["researcher", "wiki_curator", "输入:", "返回:"]:
        assert token in cat, f"catalog 缺 {token}:\n{cat}"

    # 6) 上下文隔离：两次派发 = 两个新实例
    creg = AgentRegistry([
        AgentSpec(
            name="counter", description="d", input_contract="i", output_contract="o",
            factory=lambda config=None, websocket=None: CountingAgent(config, websocket),
        ),
    ])
    ctool = DispatchAgentTool(creg)
    CountingAgent.instances = 0
    await ctool.execute(agent="counter", prompt="a")
    await ctool.execute(agent="counter", prompt="b")
    assert CountingAgent.instances == 2, f"每次派发应新建实例，实得 {CountingAgent.instances}"

    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
