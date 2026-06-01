"""
方案 1 · 步 1 验证：LLM 调用的无损增量追踪。

证明的是「问题被解决」——此前永远无法复盘的三样东西，现在原样躺在落盘文件里：
  1. 每个 agent 实际用的完整 system prompt；
  2. LLM 原样输出的 tool_call 完整参数（尤其 save_report 那整篇报告——dispatch 只回截断预览）；
  3. LLM 实际收到的工具结果原文（检索条目，而非「N 条 / M 字」摘要）。
并核对增量不重不漏：每条消息只出现一次，nudge 注入作为独立记录在位。

不发网络：FakeLLM 实现 provider 的真实扩展点 _chat_impl（故走 BaseLLM.chat 这一咽喉、
触发 tap），驱动「真实的」ResearchAgent.run() 内部 ReAct 循环。
"""

import asyncio
import contextlib
import json
import sys
import tempfile
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import replay  # noqa: E402  （scripts/replay.py）
import core.trace as trace  # noqa: E402
import agents.coordinator_agent as coord_module  # noqa: E402
import agents.research_agent as research_module  # noqa: E402
from agents.coordinator_agent import CoordinatorAgent  # noqa: E402
from agents.research_agent import ResearchAgent, SYSTEM_PROMPT_TEMPLATE  # noqa: E402
from core.agent_interface import AgentEvent, AgentInterface  # noqa: E402
from core.llm.base import BaseLLM, ChatMessage, LLMResponse, ToolCall, TokenUsage  # noqa: E402
from core.registry import AgentRegistry, AgentSpec  # noqa: E402
from core.retrievers.base import BaseRetriever, SearchResult  # noqa: E402
from core.runner import run_agent  # noqa: E402


class FakeLLM(BaseLLM):
    """实现 _chat_impl（而非 chat）——即真 provider 的扩展点，故被基类 tap 追踪。"""

    provider_name = "fake"

    def __init__(self, trajectory: List[dict], model: str = "fake-model"):
        super().__init__(model=model)
        self._traj = list(trajectory)
        self._idx = 0

    async def _chat_impl(self, messages, **kwargs):
        i = min(self._idx, len(self._traj) - 1) if self._traj else 0
        item = self._traj[i] if self._traj else {"content": ""}
        self._idx += 1
        tool_calls = item.get("tool_calls", [])
        usage = TokenUsage(input_tokens=5, output_tokens=10)
        await self._record_usage(usage)
        return LLMResponse(
            content=item.get("content", ""),
            usage=usage,
            model=self.model,
            provider=self.provider_name,
            tool_calls=tool_calls,
            stop_reason="tool_calls" if tool_calls else "stop",
        )


class FakeRetriever(BaseRetriever):
    source_name = "arxiv"

    def __init__(self, results: List[SearchResult]):
        self._results = list(results)

    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        return list(self._results[:max_results])


# 落在工具结果原文里的「指纹」字符串，用来证明检索原文被完整保留
RESULT_FINGERPRINT = "FINGERPRINT-扩散模型可控生成-9f3a"
FULL_REPORT = (
    "# 扩散模型综述\n\n这是一篇完整报告的开头总览。\n\n"
    "## 关键发现\n1. 发现 A 的若干细节……\n2. 发现 B 的若干细节……\n\n"
    "## 结论\n综合来看，主流方向是可控生成。"
)


def _read_records(path: Path) -> List[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


async def case_lossless_recovery():
    """一次真实 ReAct：search_papers → save_report(整篇) → 停。核对三样都被原样保留。"""
    results = [SearchResult(
        title="Controllable Diffusion", url="http://x/1",
        snippet=RESULT_FINGERPRINT, source="arxiv",
    )]
    smart_traj = [
        {"tool_calls": [ToolCall(id="c1", name="search_papers",
                                 arguments={"query": "diffusion models"})]},
        {"tool_calls": [ToolCall(id="c2", name="save_report",
                                 arguments={"filename": "diffusion.md", "content": FULL_REPORT})]},
        {"content": "已完成并落盘。"},
    ]
    research_module.get_llm = lambda tier=None, config=None: FakeLLM(smart_traj)
    ResearchAgent._make_retrievers = lambda self: [FakeRetriever(results)]

    async for ev in ResearchAgent().run("调研扩散模型可控生成"):
        if ev.type == "result":
            break

    recs = _read_records(trace.current_path())

    # —— 1. 完整 system prompt 被保留（此前完全不记录）——
    sys_recs = [r for r in recs if r["kind"] == "msg" and r["payload"]["role"] == "system"]
    assert len(sys_recs) == 1, f"system prompt 应恰好记一次，实得 {len(sys_recs)}"
    expected_sys = SYSTEM_PROMPT_TEMPLATE.split("{today}")[0]
    assert expected_sys[:60] in sys_recs[0]["payload"]["content"], "system prompt 应原样保留"

    # —— 2. tool_call 完整参数：save_report 的整篇 content（dispatch 只回截断预览）——
    save_calls = [
        tc for r in recs if r["kind"] == "response"
        for tc in r["payload"].get("tool_calls", []) if tc["name"] == "save_report"
    ]
    assert len(save_calls) == 1, "save_report 调用应被记一次"
    assert save_calls[0]["arguments"]["content"] == FULL_REPORT, \
        "save_report 的整篇报告参数应原样保留，而非摘要/截断"

    # —— 3. 工具结果原文：检索条目全文（此前只记「N 条 / M 字」）——
    tool_recs = [r for r in recs if r["kind"] == "msg" and r["payload"]["role"] == "tool"]
    assert any(RESULT_FINGERPRINT in r["payload"]["content"] for r in tool_recs), \
        "检索结果原文（含指纹）应原样保留在 tool 记录里"

    # —— 增量不重不漏：assistant 答复只经 response 记录，不在输入尾巴里重复 ——
    assert not any(r["kind"] == "msg" and r["payload"]["role"] == "assistant" for r in recs), \
        "assistant 不该作为 msg 重复出现（已由 response 承载）"
    seqs = [r["seq"] for r in recs]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seq 应单调且唯一"
    # 正常 append-only 流不该误触发步4 守卫
    assert not any(r["kind"] == "context_edited" for r in recs), \
        "append-only 流不应产生 context_edited"
    print(f"  case_lossless_recovery OK（{len(recs)} 条记录）")


async def case_injection_captured():
    """nudge 注入是一条新增 user 消息 → 应作为独立增量记录被捕获（不丢、不混入别处）。"""
    # 同一 (search_papers, query) 连发 DUP_THRESHOLD 次触发 nudge 注入
    dup = ToolCall(id="c", name="search_papers", arguments={"query": "same"})
    smart_traj = (
        [{"tool_calls": [dup]}] * research_module.DUP_THRESHOLD
        + [{"content": "停。"}]
    )
    results = [SearchResult(title="P", url="u", snippet="s", source="arxiv")]
    research_module.get_llm = lambda tier=None, config=None: FakeLLM(smart_traj)
    ResearchAgent._make_retrievers = lambda self: [FakeRetriever(results)]

    async for ev in ResearchAgent().run("会触发 nudge 的任务"):
        if ev.type == "result":
            break

    recs = _read_records(trace.current_path())
    user_texts = [r["payload"]["content"] for r in recs
                  if r["kind"] == "msg" and r["payload"]["role"] == "user"]
    assert any("似乎卡住了" in t for t in user_texts), \
        f"nudge 注入应作为独立 user 记录被捕获：{user_texts}"
    print("  case_injection_captured OK")


class LeafAgent(AgentInterface):
    """最小 spoke：调一次 LLM 后给 result。每个实例自带 FakeLLM（并发隔离）。"""

    name = "LeafAgent"

    async def run(self, task, **kwargs):
        llm = FakeLLM([{"content": f"leaf 答复：{task}"}])
        resp = await llm.chat([ChatMessage(role="user", content=task)])
        yield AgentEvent(type="result", content=resp.content,
                         metadata={"status": "ok", "summary": "leaf done"})


def _leaf_registry() -> AgentRegistry:
    return AgentRegistry([AgentSpec(
        name="leaf", description="d", input_contract="i", output_contract="o",
        factory=lambda config=None, websocket=None: LeafAgent(),
    )])


async def case_hierarchy():
    """hub→spoke 经 run_agent 这一咽喉成树：hub 无父；spoke 的 parent 指向 hub；
    并发两个 spoke 各自独立 run_id，互不串味。"""
    hub_traj = [
        {"tool_calls": [
            ToolCall(id="d0", name="dispatch_agent", arguments={"agent": "leaf", "prompt": "子任务 A"}),
            ToolCall(id="d1", name="dispatch_agent", arguments={"agent": "leaf", "prompt": "子任务 B"}),
        ]},
        {"content": "## 汇总\n两路都完成。"},
    ]
    coord_module.get_llm = lambda *a, **k: FakeLLM(hub_traj)

    async for ev in run_agent(CoordinatorAgent(config=None), "顶层任务", registry=_leaf_registry()):
        if ev.type == "status" and ev.content in ("done", "error"):
            break

    recs = _read_records(trace.current_path())
    assert all("run_id" in r for r in recs), "步2 后每条记录都应带 run_id"

    hub_runs = {r["run_id"] for r in recs if r.get("agent") == "CoordinatorAgent"}
    leaf_runs = {r["run_id"] for r in recs if r.get("agent") == "LeafAgent"}
    assert len(hub_runs) == 1, f"hub 应恰好一个 run，实得 {hub_runs}"
    hub_id = next(iter(hub_runs))

    # hub 是根：无 parent
    assert all("parent_run_id" not in r for r in recs if r["run_id"] == hub_id), \
        "hub 记录不应有 parent_run_id"
    # 两个并发 spoke：各自独立 run_id，且都挂在 hub 下
    assert len(leaf_runs) == 2, f"两个并发 spoke 应有 2 个独立 run，实得 {leaf_runs}"
    for r in recs:
        if r.get("agent") == "LeafAgent":
            assert r.get("parent_run_id") == hub_id, \
                f"spoke 的 parent 应指向 hub：{r.get('parent_run_id')} != {hub_id}"
    print(f"  case_hierarchy OK（hub={hub_id}, spokes={leaf_runs}）")


async def case_replay():
    """replay 把 trace 还原成人读 transcript：hub→spoke 树状缩进 + 三样无损内容都在。"""
    # 复用层级场景：hub 派一个 leaf
    hub_traj = [
        {"tool_calls": [ToolCall(id="d0", name="dispatch_agent",
                                 arguments={"agent": "leaf", "prompt": "子任务 X"})]},
        {"content": "## 汇总\n完成。"},
    ]
    coord_module.get_llm = lambda *a, **k: FakeLLM(hub_traj)
    async for ev in run_agent(CoordinatorAgent(config=None), "顶层任务", registry=_leaf_registry()):
        if ev.type == "status" and ev.content in ("done", "error"):
            break

    text = replay.render_tree(replay.load_records(trace.current_path()))

    # 树状：hub 与 spoke 各有 run 头，且 spoke 缩进更深（挂在 hub 下）
    hub_line = next(l for l in text.splitlines() if "agent=CoordinatorAgent" in l)
    leaf_line = next(l for l in text.splitlines() if "agent=LeafAgent" in l)
    indent = lambda s: len(s) - len(s.lstrip())
    assert indent(leaf_line) > indent(hub_line), "spoke run 应缩进在 hub 之下"

    # 无损内容在 transcript 里：hub 发给 spoke 的真实 prompt、spoke 的答复、汇总
    assert "子任务 X" in text, "hub 发给 spoke 的真实 prompt 应出现"
    assert "leaf 答复：子任务 X" in text, "spoke 的答复应出现"
    assert "## 汇总" in text, "hub 末轮答复应出现"

    # CLI 端到端：能选到最新文件并打印（不抛异常、有内容）
    rc = replay.main([str(trace.current_path())])
    assert rc == 0
    print("  case_replay OK")


async def case_append_only_guard():
    """步4：前缀被改写/截断（未来上下文压缩）时落 context_edited + 快照，不静默丢数据。"""
    sys0 = ChatMessage(role="system", content="原系统提示")
    user0 = ChatMessage(role="user", content="原任务")
    asst1 = ChatMessage(role="assistant", content="思考",
                        tool_calls=[ToolCall(id="c1", name="search", arguments={"q": "x"})])
    tool1 = ChatMessage(role="tool", content="检索结果原文", tool_call_id="c1")

    # —— 改写：第三次请求时，前缀第 0 位 system 被换掉、整体压缩成 2 条 ——
    tr = trace.LLMTracer("fake", "fake-model")
    sid = tr.stream_id
    tr.on_request([sys0, user0])                  # append: 2 条 msg
    tr.on_request([sys0, user0, asst1, tool1])    # append: tool1（跳过 asst1）
    compacted_user = ChatMessage(role="user", content="压缩后的新摘要任务")
    tr.on_request([ChatMessage(role="system", content="改写后的系统提示"), compacted_user])

    recs = [r for r in _read_records(trace.current_path()) if r.get("stream_id") == sid]
    # 改写前没有守卫记录
    pre = [r for r in recs if r["kind"] == "context_edited"]
    assert len(pre) == 1, f"应恰好一条 context_edited，实得 {len(pre)}"
    ce = pre[0]["payload"]
    assert ce["diverged_at"] == 0 and ce["old_len"] == 4 and ce["new_len"] == 2, ce
    assert len(ce["snapshot"]) == 2, "快照应含改写后的全部当前消息"
    # 新内容不被静默丢失：可从快照恢复
    assert any("压缩后的新摘要任务" in m["content"] for m in ce["snapshot"]), \
        "压缩后的新消息应在快照里，未被增量逻辑漏掉"

    # —— 纯截断：前缀一致但变短 → diverged_at = 新长度 ——
    tr2 = trace.LLMTracer("fake", "fake-model")
    sid2 = tr2.stream_id
    tr2.on_request([sys0, user0, asst1, tool1])
    tr2.on_request([sys0, user0])                 # 砍掉后两条，前缀不变
    ce2 = [r for r in _read_records(trace.current_path())
           if r.get("stream_id") == sid2 and r["kind"] == "context_edited"]
    assert len(ce2) == 1 and ce2[0]["payload"]["diverged_at"] == 2, ce2
    print("  case_append_only_guard OK")


async def main() -> None:
    for case in (case_lossless_recovery, case_injection_captured, case_hierarchy,
                 case_replay, case_append_only_guard):
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            trace.reset()
            trace.configure(dir=str(Path(tmp) / "traces"), enabled=True)
            await case()
            trace.reset()
    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
