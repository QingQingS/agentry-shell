"""
Step 4 验证脚本 —— OrchestratorAgent 的编排契约（离线，stub 掉分类器与 worker）。

校验：session_id 稳定、三轴路由分发正确、kwargs（mode/target/background/context）正确、
事件转发、写回（save_report + add_turn）、carry_context 门控背景注入、窗口管理串联。
    $PY tests/check_orchestrator.py
全部断言通过则打印 OK。
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents.orchestrator_agent as orch_mod  # noqa: E402
from agents.orchestrator_agent import OrchestratorAgent  # noqa: E402
from core.agent_interface import AgentEvent  # noqa: E402
from core.intent import IntentResult  # noqa: E402
from core.session import SessionManager  # noqa: E402

# 记录每个 worker 实例收到的 (kind, task, kwargs)
CAPTURED: list = []


class FakeResearchAgent:
    def __init__(self, config=None, websocket=None):
        pass

    async def run(self, task, **kwargs):
        CAPTURED.append(("research", task, kwargs))
        yield AgentEvent(type="log", content="fake research log")
        yield AgentEvent(type="result", content=f"REPORT::{task}")


class FakeChatAgent:
    def __init__(self, config=None, websocket=None):
        pass

    async def run(self, task, **kwargs):
        CAPTURED.append(("chat", task, kwargs))
        yield AgentEvent(type="result", content=f"ANSWER::{task}")


class FakeWikiAgent:
    def __init__(self, config=None, websocket=None):
        pass

    async def run(self, task, **kwargs):
        CAPTURED.append(("wiki", task, kwargs))
        yield AgentEvent(type="log", content="fake wiki log")
        yield AgentEvent(type="result", content=f"CURATED::{task}")


def _make_classifier(script):
    """按调用次序返回预设 IntentResult。"""
    seq = iter(script)

    async def fake_classify(user_input, session_context, llm):
        return next(seq)

    return fake_classify


async def run_turn(agent, task):
    events = []
    async for ev in agent.run(task):
        events.append(ev)
    return events


async def main_async() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # 注入：临时 SessionManager + stub 分类器 / worker / get_llm
        OrchestratorAgent._session_manager = SessionManager(reports_dir=tmp, window_size=2)
        orch_mod.ResearchAgent = FakeResearchAgent
        orch_mod.ChatAgent = FakeChatAgent
        orch_mod.WikiAgent = FakeWikiAgent
        orch_mod.get_llm = lambda **kw: None  # classify 已 stub，不需要真 LLM

        orch_mod.classify_intent = _make_classifier([
            IntentResult("research", "survey", "2026 LLM", carry_context=False),       # 轮1
            IntentResult("research", "code_search", "Transformer impl", carry_context=True),  # 轮2
            IntentResult("chat", "survey", "", carry_context=True),                    # 轮3
            IntentResult("wiki", "survey", "", carry_context=False, files=["./reports/x.md"]),  # 轮4
        ])

        agent = OrchestratorAgent(config=None, websocket=None)  # id(None) 恒定 → 同一 session
        sid = agent._session_id()
        sm = OrchestratorAgent._session_manager

        # 轮1：research/survey/carry=false
        events = await run_turn(agent, "研究 2026 大模型")
        assert any(e.type == "result" and e.content == "REPORT::2026 LLM" for e in events), "应转发 worker 的 result"
        kind, wtask, kw = CAPTURED[-1]
        assert kind == "research" and wtask == "2026 LLM", "应路由到 research，task=target"
        assert kw["mode"] == "survey"
        assert kw["background_context"] == "", "carry=false 不带背景"
        session = sm.get_or_create(sid)
        assert len(session.reports) == 1 and session.reports[-1].content == "REPORT::2026 LLM"
        assert len(session.turns) == 1 and session.turns[-1].route == "research"
        assert session.turns[-1].agent_response == sm.get_or_create(sid).reports[-1].description, "research turn 用快照作短文本"

        # 轮2：research/code_search/carry=true → 带轮1报告正文作背景
        await run_turn(agent, "找它的开源实现")
        kind, wtask, kw = CAPTURED[-1]
        assert kind == "research" and wtask == "Transformer impl"
        assert kw["mode"] == "code_search"
        assert kw["background_context"] == "REPORT::2026 LLM", "carry=true 应带最近一份报告正文"
        assert len(session.reports) == 2

        # 轮3：chat/carry=true → context = 最近一份报告（轮2）正文
        await run_turn(agent, "刚才说的是什么意思")
        kind, wtask, kw = CAPTURED[-1]
        assert kind == "chat" and wtask == "刚才说的是什么意思", "chat 用原始 task，不用 target"
        assert kw["context"] == "REPORT::Transformer impl", "chat 带最近报告作 context"
        assert len(session.reports) == 2, "chat 不新增报告"
        assert len(session.turns) == 3 and session.turns[-1].route == "chat"
        assert session.turns[-1].agent_response == "ANSWER::刚才说的是什么意思"

        # 轮4：wiki → 透传 files，落 turn（不新增报告）
        events = await run_turn(agent, "把 ./reports/x.md 存进 wiki")
        assert any(e.type == "result" and e.content == "CURATED::把 ./reports/x.md 存进 wiki" for e in events), "应转发 wiki worker 的 result"
        kind, wtask, kw = CAPTURED[-1]
        assert kind == "wiki" and wtask == "把 ./reports/x.md 存进 wiki", "wiki 用原始 task"
        assert kw["files"] == ["./reports/x.md"], "应透传 files 给 WikiAgent"
        assert "background_context" not in kw and "context" not in kw, "wiki 不注入背景/context"
        assert len(session.reports) == 2, "wiki 不新增报告"
        assert len(session.turns) == 4 and session.turns[-1].route == "wiki"
        assert session.turns[-1].agent_response == "CURATED::把 ./reports/x.md 存进 wiki"

        # session 隔离：不同 websocket → 不同 id → 不同 session
        other = OrchestratorAgent(config=None, websocket=object())
        assert other._session_id() != sid

    print("OK")


if __name__ == "__main__":
    asyncio.run(main_async())
