"""
v2 Step 2 验证脚本 —— CoordinatorAgent ReAct 循环（离线）。

用脚本化 FakeLLM 确定性验证循环逻辑，不发网络请求、不调真实 spoke
（注册表里放零依赖的 EchoAgent 当 spoke）：
    $PY tests/check_coordinator.py
全部断言通过则打印 OK。

覆盖：
  1. 退化路由：一轮 dispatch echo → 拿 observation → 末轮输出 markdown，result 收尾。
  2. spokes_used 记录在 result 事件 metadata（成功派发才计入）。
  3. 零派发（chat）：首轮就出文字 → 直接作答，不派 spoke。
  4. 错误转 observation：派未知 agent → Error 回灌后循环继续、能正常收尾（不崩）。
  5. MAX_ROUNDS 触顶：LLM 一直发 tool_call → 强制结束并给兜底 result。
"""

from __future__ import annotations

import asyncio
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents.coordinator_agent as coord_mod  # noqa: E402
from agents.coordinator_agent import MAX_ROUNDS, CoordinatorAgent  # noqa: E402
from agents.echo_agent import EchoAgent  # noqa: E402
from core.agent_interface import AgentEvent, AgentInterface  # noqa: E402
from core.llm.base import BaseLLM, LLMResponse, ToolCall, TokenUsage  # noqa: E402
from core.registry import AgentRegistry, AgentSpec  # noqa: E402

_ids = itertools.count()


def tc(name: str, **args) -> ToolCall:
    return ToolCall(id=f"c{next(_ids)}", name=name, arguments=args)


def tool_resp(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(content="", usage=TokenUsage(), model="fake", provider="fake",
                       tool_calls=list(calls), stop_reason="tool_calls")


def text_resp(text: str) -> LLMResponse:
    return LLMResponse(content=text, usage=TokenUsage(), model="fake", provider="fake",
                       tool_calls=[], stop_reason="stop")


class FakeLLM(BaseLLM):
    """按脚本逐轮返回；脚本耗尽后重复最后一条（用于 MAX_ROUNDS 测试）。"""

    provider_name = "fake"

    def __init__(self, script):
        super().__init__(model="fake", api_key=None)
        self.script = script
        self.turns = []

    async def chat(self, messages, *, temperature=None, max_tokens=None, tools=None, **kwargs):
        self.turns.append(list(messages))
        resp = self.script[min(len(self.turns) - 1, len(self.script) - 1)]
        await self._record_usage(TokenUsage(input_tokens=1, output_tokens=1))
        return resp


def echo_registry() -> AgentRegistry:
    return AgentRegistry([
        AgentSpec(
            name="echo", description="演示 spoke", input_contract="prompt=任意",
            output_contract="回显",
            factory=lambda config=None, websocket=None: EchoAgent(config=config, websocket=websocket),
        ),
    ])


class ContextSpyAgent(AgentInterface):
    """把收到的 (prompt, context) 记进类级列表，验证串行依赖时 context 注入下游。"""

    received = []
    name = "ContextSpyAgent"

    async def run(self, task, **kwargs):
        ContextSpyAgent.received.append((task, kwargs.get("context", "")))
        yield AgentEvent(type="result", content="done")


def spy_registry() -> AgentRegistry:
    return AgentRegistry([
        AgentSpec(
            name="spy", description="d", input_contract="i", output_contract="o",
            factory=lambda config=None, websocket=None: ContextSpyAgent(),
        ),
    ])


def patch_llm(fake: FakeLLM):
    coord_mod.get_llm = lambda *a, **k: fake  # type: ignore


async def collect(script, **run_kwargs):
    patch_llm(FakeLLM(script))
    agent = CoordinatorAgent(config=None)
    return [ev async for ev in agent.run(run_kwargs.pop("task", "任务"), **run_kwargs)]


def result_event(events):
    rs = [e for e in events if e.type == "result"]
    assert len(rs) == 1, f"应恰有 1 个 result 事件，实得 {len(rs)}"
    return rs[0]


async def main() -> None:
    reg = echo_registry()

    # 1+2) 退化路由：dispatch echo → 综合答复
    events = await collect(
        [tool_resp(tc("dispatch_agent", agent="echo", prompt="你好")),
         text_resp("## 最终答复\n已完成。")],
        registry=reg, task="跑个 echo",
    )
    res = result_event(events)
    assert res.content == "## 最终答复\n已完成。", res.content
    assert res.metadata.get("spokes_used") == ["echo"], res.metadata
    # 派发动作有 trace=action 日志，spoke 归属正确
    actions = [e for e in events if e.metadata.get("trace") == "action"]
    assert any(a.metadata.get("spoke") == "echo" for a in actions), "缺 echo 派发的 action 日志"

    # 3) 零派发 chat：首轮直接文字
    events = await collect([text_resp("直接回答你。")], registry=reg, task="一句闲聊")
    res = result_event(events)
    assert res.content == "直接回答你。"
    assert res.metadata.get("spokes_used") == [], "零派发时 spokes_used 应为空"

    # 4) 错误转 observation：未知 agent → Error 回灌后仍能收尾
    events = await collect(
        [tool_resp(tc("dispatch_agent", agent="ghost", prompt="x")),
         text_resp("已处理（部分）。")],
        registry=reg, task="派个不存在的",
    )
    res = result_event(events)
    assert res.content == "已处理（部分）。"
    assert res.metadata.get("spokes_used") == [], "失败派发不应计入 spokes_used"

    # 6) 并行扇入：一轮两个 echo 派发 → 并发执行、事件带各自 spoke_id、两 observation 回填
    events = await collect(
        [tool_resp(tc("dispatch_agent", agent="echo", prompt="A"),
                   tc("dispatch_agent", agent="echo", prompt="B")),
         text_resp("## 汇总\n两路都完成。")],
        registry=reg, task="并行两路",
    )
    res = result_event(events)
    assert res.content == "## 汇总\n两路都完成。"
    assert res.metadata.get("spokes_used") == ["echo", "echo"], res.metadata
    seen_ids = {e.metadata.get("spoke_id") for e in events if e.metadata.get("spoke_id")}
    assert {"echo#0", "echo#1"} <= seen_ids, f"两个 spoke_id 都应出现: {seen_ids}"
    forwarded = [e for e in events
                 if e.metadata.get("spoke_id") and e.metadata.get("trace") not in ("action", "leaf")]
    assert any(e.metadata.get("spoke_id") == "echo#0" for e in forwarded), "echo#0 内部事件应冒泡"
    assert any(e.metadata.get("spoke_id") == "echo#1" for e in forwarded), "echo#1 内部事件应冒泡"

    # 7) 串行依赖：A 的结果蒸馏进 B 的 context（分轮），下游 spoke 确实收到 context
    ContextSpyAgent.received = []
    events = await collect(
        [tool_resp(tc("dispatch_agent", agent="spy", prompt="研究 A")),
         tool_resp(tc("dispatch_agent", agent="spy", prompt="基于 A 做 B", context="A 的蒸馏结论")),
         text_resp("## 终稿\nA→B 串起来了。")],
        registry=spy_registry(), task="先 A 后 B",
    )
    res = result_event(events)
    assert res.content == "## 终稿\nA→B 串起来了。"
    assert res.metadata.get("spokes_used") == ["spy", "spy"], res.metadata
    # 上游空 context、下游收到注入的 context；顺序正确；各 spoke 只拿到自己的入参（双向隔离）
    assert ContextSpyAgent.received == [
        ("研究 A", ""), ("基于 A 做 B", "A 的蒸馏结论")
    ], ContextSpyAgent.received

    # 5) MAX_ROUNDS 触顶：脚本永远发 tool_call
    events = await collect(
        [tool_resp(tc("dispatch_agent", agent="echo", prompt="loop"))],
        registry=reg, task="无限派发",
    )
    res = result_event(events)
    assert res.content, "触顶也应有兜底 result"
    # 触顶意味着 echo 被派发了 MAX_ROUNDS 次
    assert res.metadata.get("spokes_used") == ["echo"] * MAX_ROUNDS, res.metadata.get("spokes_used")

    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
