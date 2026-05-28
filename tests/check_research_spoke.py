"""
v2 Step 5 + Step 5.5 验证：ResearchAgent spoke 契约 + 内部 ReAct 循环。

不发网络请求：FakeLLM 按预设 trajectory（每条决定 content + tool_calls）+ FakeRetriever，
驱动 ResearchAgent 内部 ReAct 循环走完整路径。

覆盖（按对外契约组织——内部 ReAct 化对外不可见）：
  1. case_broad_survey：LLM 调 do_broad_survey 一次后写 final → status=ok +
     summary=冒头段 + dispatch observation 含完整 final 报告。
  2. case_atomic_search：LLM 自决多次 search_papers 后写 final（步5.5 新能力，
     不再走复合工具的固定流水线）。
  3. case_degenerate：LLM 调 search_papers 但检索全空 → status=degenerate +
     标准 summary。
  4. case_context_kwarg：context（新）/ background_context（旧 v1）kwarg 都触发
     背景注入 log（过渡期兼容）。
"""

import asyncio
import contextlib
import json
import sys
import tempfile
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents.research_agent as research_module  # noqa: E402
from agents.research_agent import ResearchAgent  # noqa: E402
from core.dispatch import DispatchAgentTool  # noqa: E402
from core.llm.base import BaseLLM, LLMResponse, ToolCall, TokenUsage  # noqa: E402
from core.registry import AgentRegistry, AgentSpec  # noqa: E402
from core.retrievers.base import BaseRetriever, SearchResult  # noqa: E402


class FakeLLM(BaseLLM):
    """按预设 trajectory 返回 LLMResponse。

    trajectory[i] = {"content": str, "tool_calls": List[ToolCall]?}
    超过末尾后重复最后一项（防止下标越界，仍能让循环自然停止）。
    """

    provider_name = "fake"

    def __init__(self, trajectory: List[dict], model: str = "fake-model"):
        super().__init__(model=model)
        self._traj = list(trajectory)
        self._idx = 0

    async def chat(self, messages, **kwargs):
        i = min(self._idx, len(self._traj) - 1) if self._traj else 0
        item = self._traj[i] if self._traj else {"content": ""}
        self._idx += 1
        content = item.get("content", "")
        tool_calls = item.get("tool_calls", [])
        usage = TokenUsage(input_tokens=5, output_tokens=10)
        await self._record_usage(usage)
        return LLMResponse(
            content=content,
            usage=usage,
            model=self.model,
            provider=self.provider_name,
            tool_calls=tool_calls,
            stop_reason="tool_calls" if tool_calls else "stop",
        )


class FakeRetriever(BaseRetriever):
    def __init__(self, results: List[SearchResult], source_name: str = "arxiv"):
        self._results = list(results)
        self.source_name = source_name

    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        return list(self._results[:max_results])


SAMPLE_FINAL = (
    "这是开头总览：基于检索到的资料，主流方向是 X。\n"
    "\n"
    "## 分点综合\n"
    "1. 发现 A：…\n"
    "2. 发现 B：…\n"
    "\n"
    "## 结论\n"
    "综合来看，主流方向是 X。"
)


def _install_fakes(fast_traj, smart_traj, results, source_names=("arxiv",)):
    fast = FakeLLM(fast_traj)
    smart = FakeLLM(smart_traj)
    original_get_llm = research_module.get_llm

    def fake_get_llm(tier=None, config=None):
        return smart if tier == "smart" else fast

    research_module.get_llm = fake_get_llm

    original_make = ResearchAgent._make_retrievers
    ResearchAgent._make_retrievers = lambda self: [
        FakeRetriever(results, source_name=name) for name in source_names
    ]

    def restore():
        research_module.get_llm = original_get_llm
        ResearchAgent._make_retrievers = original_make

    return restore


def _researcher_registry() -> AgentRegistry:
    return AgentRegistry([
        AgentSpec(
            name="researcher", description="d", input_contract="i", output_contract="o",
            factory=lambda config=None, websocket=None: ResearchAgent(
                config=config, websocket=websocket
            ),
        ),
    ])


async def case_broad_survey():
    """LLM 第 1 步调 do_broad_survey → 第 2 步写 final markdown。
    smart 总共被调 3 次（顶层 step1 tool_call、tool 内部综合、顶层 step2 final）；
    fast 被调 4 次（拆问 1 + 3 个子问题摘要）。
    """
    results = [
        SearchResult(title=f"Paper {i}", url=f"http://x/{i}", snippet="abs", source="arxiv")
        for i in range(2)
    ]
    smart_traj = [
        {"tool_calls": [ToolCall(
            id="c1", name="do_broad_survey",
            arguments={"topic": "RL 最新进展", "background": ""},
        )]},
        {"content": "do_broad_survey 内部综合的报告 markdown"},  # 工具内部 smart.chat
        {"content": SAMPLE_FINAL},                                # 顶层 final
    ]
    fast_traj = [
        {"content": json.dumps(["q1 about X", "q2 about Y", "q3 about Z"])},
        {"content": "子问题 1 摘要"},
        {"content": "子问题 2 摘要"},
        {"content": "子问题 3 摘要"},
    ]
    restore = _install_fakes(fast_traj, smart_traj, results)
    try:
        obs = await DispatchAgentTool(_researcher_registry()).execute(
            agent="researcher", prompt="调研 RL 最新进展", context=""
        )
    finally:
        restore()

    assert "[researcher] status=ok" in obs, obs
    head = obs.split("---", 1)[0]
    assert "summary: 这是开头总览" in head, f"summary 应取自 final 冒头段：\n{head}"
    assert "分点综合" not in head, "summary 不该越界到下一段"
    assert "\n---\nreport:\n" in obs, "observation 应含 report 段"
    body = obs.split("\nreport:\n", 1)[1]
    assert body.strip() == SAMPLE_FINAL.strip(), "report 段应是 final 报告原文"
    print("  case_broad_survey OK")


async def case_atomic_search():
    """步5.5 核心新能力：LLM 不走复合工具，自决两次原子 search_papers 后综合。"""
    results = [SearchResult(title="P A", url="http://a", snippet="abs", source="arxiv")]
    smart_traj = [
        {"tool_calls": [ToolCall(id="c1", name="search_papers",
                                 arguments={"query": "topic A"})]},
        {"tool_calls": [ToolCall(id="c2", name="search_papers",
                                 arguments={"query": "topic B"})]},
        {"content": SAMPLE_FINAL},
    ]
    fast_traj = []  # 不走 do_broad_survey，fast 不用
    restore = _install_fakes(fast_traj, smart_traj, results)
    try:
        obs = await DispatchAgentTool(_researcher_registry()).execute(
            agent="researcher", prompt="读一些 RL 论文然后总结"
        )
    finally:
        restore()

    assert "[researcher] status=ok" in obs, obs
    assert "summary: 这是开头总览" in obs.split("---", 1)[0], obs
    body = obs.split("\nreport:\n", 1)[1]
    assert body.strip() == SAMPLE_FINAL.strip()
    print("  case_atomic_search OK")


async def case_degenerate():
    """LLM 调 search_papers 但检索全空 → 工具 obs '(未检索到相关论文)' →
    循环代码侧统计 retrieval_hits=0 → status=degenerate。
    """
    smart_traj = [
        {"tool_calls": [ToolCall(id="c1", name="search_papers",
                                 arguments={"query": "obscure topic"})]},
        {"content": "未找到相关资料，无法生成完整报告。"},
    ]
    restore = _install_fakes([], smart_traj, results=[])
    try:
        obs = await DispatchAgentTool(_researcher_registry()).execute(
            agent="researcher", prompt="冷门主题"
        )
    finally:
        restore()
    assert "[researcher] status=degenerate" in obs, obs
    assert "summary: （未检索到相关结果）" in obs, obs
    # degenerate 也带 report 段（hub 自决弃用——dispatch 不挑挑拣拣）
    assert "\n---\nreport:\n" in obs
    print("  case_degenerate OK")


async def case_context_kwarg():
    """新 context 与旧 v1 background_context kwarg 都触发"携带上轮报告"log。"""
    results = [SearchResult(title="P", url="u", snippet="s", source="arxiv")]
    smart_traj = [{"content": "（直接答复，不调工具）"}]
    for label, kwargs in [
        ("context (new)", {"context": "上一轮：xxx"}),
        ("background_context (legacy)", {"background_context": "上一轮：xxx"}),
    ]:
        restore = _install_fakes([], smart_traj, results)
        logs = []
        try:
            agent = ResearchAgent()
            async for ev in agent.run("topic", **kwargs):
                if ev.type == "log":
                    logs.append(ev.content)
                if ev.type == "result":
                    break
        finally:
            restore()
        assert any("携带上轮报告" in l for l in logs), f"[{label}] 应触发 background log: {logs}"
    print("  case_context_kwarg OK")


async def case_save_report():
    """LLM 在 do_broad_survey 后调 save_report 落盘 → metadata.artifact_path 被填，
    dispatch observation 含 artifact: 行；save_report 的 content 优先于末轮 text 作 final。"""
    results = [SearchResult(title="P", url="u", snippet="s", source="arxiv")]
    smart_traj = [
        {"tool_calls": [ToolCall(id="c1", name="do_broad_survey",
                                 arguments={"topic": "RL", "background": ""})]},
        {"content": "do_broad_survey 内部综合报告"},
        {"tool_calls": [ToolCall(id="c2", name="save_report",
                                 arguments={"filename": "rl-survey.md",
                                            "content": SAMPLE_FINAL})]},
        {"content": "已落盘。"},   # 末轮短确认，不该覆盖 save_report 的 content
    ]
    fast_traj = [
        {"content": json.dumps(["q1", "q2", "q3"])},
        {"content": "摘要 1"},
        {"content": "摘要 2"},
        {"content": "摘要 3"},
    ]
    restore = _install_fakes(fast_traj, smart_traj, results)
    try:
        obs = await DispatchAgentTool(_researcher_registry()).execute(
            agent="researcher", prompt="调研 RL"
        )
    finally:
        restore()
    assert "[researcher] status=ok" in obs, obs
    assert "artifact: reports/rl-survey.md" in obs, f"observation 应含 artifact 行:\n{obs}"
    assert Path("reports/rl-survey.md").is_file(), "save_report 真实写入文件"
    body = obs.split("\nreport:\n", 1)[1]
    assert body.strip() == SAMPLE_FINAL.strip(), \
        "report 段应是 save_report 的 content（不被末轮短文本覆盖）"
    print("  case_save_report OK")


async def case_fallback_save():
    """LLM 直接给 text final 不调 save_report → 代码兜底落盘到 reports/auto-*.md，
    artifact_path 仍出现在 metadata 与 observation。"""
    results = [SearchResult(title="P A", url="http://a", snippet="abs", source="arxiv")]
    smart_traj = [
        {"tool_calls": [ToolCall(id="c1", name="search_papers",
                                 arguments={"query": "topic"})]},
        {"content": SAMPLE_FINAL},   # 末轮直接给 final，没调 save_report
    ]
    restore = _install_fakes([], smart_traj, results)
    try:
        obs = await DispatchAgentTool(_researcher_registry()).execute(
            agent="researcher", prompt="兜底测试"
        )
    finally:
        restore()
    assert "[researcher] status=ok" in obs, obs
    artifact_line = next((l for l in obs.splitlines() if l.startswith("artifact:")), None)
    assert artifact_line, f"observation 应含 artifact 行:\n{obs}"
    fb_path = artifact_line.split("artifact:", 1)[1].strip()
    assert fb_path.startswith("reports/auto-"), f"兜底文件名应以 auto- 开头: {fb_path}"
    assert Path(fb_path).is_file(), "兜底文件真实落盘"
    body = obs.split("\nreport:\n", 1)[1]
    assert body.strip() == SAMPLE_FINAL.strip(), "兜底前 final_content 来自末轮 text"
    print("  case_fallback_save OK")


async def main() -> None:
    # 所有 case 共用 tmpdir + chdir，避免 reports/ 兜底落盘污染项目目录
    with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
        await case_broad_survey()
        await case_atomic_search()
        await case_degenerate()
        await case_context_kwarg()
        await case_save_report()
        await case_fallback_save()
        print("OK")


if __name__ == "__main__":
    asyncio.run(main())
