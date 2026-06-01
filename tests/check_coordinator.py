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
import contextlib
import itertools
import sys
import tempfile
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


class CapHitLLM(FakeLLM):
    """触顶场景：带工具时永远发 tool_call；诚实收尾探针（tools 为空）时返回进度文本。

    Coordinator 触顶后会用 tools=None 再调一次 chat 强制据实收尾——这里据此分流。
    """

    def __init__(self):
        super().__init__(script=[tool_resp(tc("dispatch_agent", agent="echo", prompt="loop"))])

    async def chat(self, messages, *, temperature=None, max_tokens=None, tools=None, **kwargs):
        if not tools:  # 诚实收尾探针：无工具 → 只能产出文本
            self.turns.append(list(messages))
            await self._record_usage(TokenUsage(input_tokens=1, output_tokens=1))
            return text_resp("## 进度收尾\n已完成：派发了 echo。\n缺：未真正解决任务。\n原因：达派发轮上限。")
        return await super().chat(messages, tools=tools, **kwargs)


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

    # 5) MAX_ROUNDS 触顶：带工具时永远发 tool_call → 强制「无工具」诚实收尾。
    #    收尾探针（tools=None）返回真实进度说明；result.content 取该说明、status=incomplete。
    patch_llm(CapHitLLM())
    agent = CoordinatorAgent(config=None)
    events = [ev async for ev in agent.run("无限派发", registry=reg)]
    res = result_event(events)
    assert res.content.startswith("## 进度收尾"), f"触顶应走诚实收尾，实得：{res.content!r}"
    assert res.metadata.get("status") == "incomplete", res.metadata
    # 触顶意味着 echo 被派发了 MAX_ROUNDS 次；诚实收尾不派发、不增计
    assert res.metadata.get("spokes_used") == ["echo"] * MAX_ROUNDS, res.metadata.get("spokes_used")

    # 8) 本地工具与 dispatch 混排：import_files → dispatch_agent(files=[...]) → text
    #    验证 import 不计入 spokes_used；本地工具 obs 正常回填；files 参数被 dispatch 接收。
    #    （staging 已降级为 wiki_curator 的 pre-hook，不再是 Coordinator 的工具——
    #     其搬运/幂等行为在 check_staging.py 的 stage_wiki_inputs 用例里覆盖。）
    with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
        ext = Path(tmp) / "ext.md"
        ext.write_text("外部文档", encoding="utf-8")

        events = await collect(
            [
                tool_resp(tc("import_files", paths=[str(ext)])),
                tool_resp(tc("dispatch_agent", agent="echo",
                             prompt="处理这个文件", files=["uploads/ext.md"])),
                text_resp("## 完成\n外部文档已入库并派给 echo 处理。"),
            ],
            registry=reg, task="把 ext.md 入库并处理",
        )
        res = result_event(events)
        assert res.content.startswith("## 完成"), res.content
        assert res.metadata.get("spokes_used") == ["echo"], \
            f"本地工具不应计入 spokes_used：{res.metadata.get('spokes_used')}"
        # 文件流真实发生
        assert Path("uploads/ext.md").is_file(), "import_files 真实复制到 uploads/"
        # action 日志正确标注工具名（非 dispatch 时用 call.name 作 label）
        action_labels = [e.metadata.get("spoke") for e in events
                         if e.metadata.get("trace") == "action"]
        assert "import_files" in action_labels, f"action 缺 import_files：{action_labels}"
        assert "echo" in action_labels, f"action 缺 echo：{action_labels}"

    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
