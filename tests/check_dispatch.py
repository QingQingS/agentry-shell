"""check_dispatch.py —— DispatchAgentTool 的离线验证（无 LLM / 无网络）。

用 FakeAgent 注册进 registry，断言 dispatch 的隔离驱动与结构化 observation：
  - status=ok + summary + artifact + report 段
  - 错误转 observation（status=error）
  - 未知 agent → error
  - files 透传给 spoke；pre_hook 改写 payload['files'] 后 spoke 看到的是改写后的值
  - pre_hook 短路（返回错误串）→ error observation，且 spoke 工厂不被调用
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import AsyncIterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.agent_interface import AgentEvent, AgentInterface  # noqa: E402
from core.dispatch import DispatchAgentTool  # noqa: E402
from core.registry import AgentRegistry, AgentSpec  # noqa: E402


class FakeAgent(AgentInterface):
    name = "FakeAgent"

    async def run(self, task: str, **kwargs) -> AsyncIterator[AgentEvent]:
        ctx = kwargs.get("context") or ""
        files = kwargs.get("files") or []
        yield AgentEvent(type="log", content="working")
        meta = {"status": "ok", "summary": "did it", "artifact_path": "reports/x.md"}
        yield AgentEvent(
            type="result",
            content=f"full report ctx={ctx} files={files}",
            metadata=meta,
        )


def _make_fake(config=None, websocket=None) -> AgentInterface:
    return FakeAgent()


def _registry(**spec_kwargs) -> AgentRegistry:
    spec = AgentSpec(
        name="fake",
        description="fake agent",
        input_contract="prompt",
        output_contract="result",
        factory=_make_fake,
        **spec_kwargs,
    )
    return AgentRegistry([spec])


async def _run() -> None:
    tool = DispatchAgentTool(_registry())

    # 正常派发：observation 含 status/summary/artifact/report
    obs = await tool.execute(agent="fake", prompt="do something")
    assert "status=ok" in obs, obs
    assert "did it" in obs, obs
    assert "artifact: reports/x.md" in obs, obs
    assert "full report" in obs, obs

    # 未知 agent → error
    obs = await tool.execute(agent="nope", prompt="x")
    assert obs.startswith("Error:") and "未知 agent" in obs, obs

    # 错误转 observation
    class BoomAgent(AgentInterface):
        name = "BoomAgent"

        async def run(self, task: str, **kwargs) -> AsyncIterator[AgentEvent]:
            raise RuntimeError("boom")
            yield  # pragma: no cover

    boom_spec = AgentSpec(
        name="boom",
        description="boom",
        input_contract="prompt",
        output_contract="result",
        factory=lambda config=None, websocket=None: BoomAgent(),
    )
    tool2 = DispatchAgentTool(AgentRegistry([boom_spec]))
    obs = await tool2.execute(agent="boom", prompt="x")
    assert "status=error" in obs, obs
    assert "boom" in obs, obs

    # pre_hook 改写 payload['files'] → spoke 看到改写后的值
    def rewrite_hook(payload: dict):
        payload["files"] = [f"staged__{f}" for f in payload.get("files") or []]
        return None

    tool3 = DispatchAgentTool(_registry(pre_hooks=[rewrite_hook]))
    obs = await tool3.execute(agent="fake", prompt="x", files=["reports/a.md"])
    assert "status=ok" in obs, obs
    assert "files=['staged__reports/a.md']" in obs, obs

    # pre_hook 短路 → error observation，且 spoke 工厂不被调用
    factory_called = {"v": False}

    def _make_tracked(config=None, websocket=None) -> AgentInterface:
        factory_called["v"] = True
        return FakeAgent()

    block_spec = AgentSpec(
        name="blocked",
        description="blocked",
        input_contract="prompt",
        output_contract="result",
        factory=_make_tracked,
        pre_hooks=[lambda payload: "bad input"],
    )
    tool4 = DispatchAgentTool(AgentRegistry([block_spec]))
    obs = await tool4.execute(agent="blocked", prompt="x", files=["reports/a.md"])
    assert "status=error" in obs, obs
    assert "bad input" in obs, obs
    assert factory_called["v"] is False, "短路后不应构造 spoke"

    print("check_dispatch OK")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
