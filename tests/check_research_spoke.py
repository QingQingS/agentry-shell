"""
v2 Step 5 验证脚本 —— ResearchAgent 作为 spoke（离线，无网络）。

用 FakeLLM + FakeRetriever 走完 survey 的「拆问 → 多源检索 → 总结 → 报告」全流程，
断言 dispatch observation 同时含：完整报告原文 + status + 冒头段 summary。

覆盖：
  1. 正常 survey 经路 → status=ok；summary=报告冒头段；observation 含完整报告原文。
  2. 全部子问题空检索 → status=degenerate；summary=「（未检索到相关结果）」。
  3. 入参兼容：context（新）与 background_context（旧 v1）都注入 prompt 作背景。

不覆盖（步5 范围外）：focused 经路（intent 退役后 dead code，步5.5 删）。
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents.research_agent as research_module  # noqa: E402
from agents.research_agent import ResearchAgent  # noqa: E402
from core.dispatch import DispatchAgentTool  # noqa: E402
from core.llm.base import BaseLLM, LLMResponse, TokenUsage  # noqa: E402
from core.registry import AgentRegistry, AgentSpec  # noqa: E402
from core.retrievers.base import BaseRetriever, SearchResult  # noqa: E402


class FakeLLM(BaseLLM):
    """按响应队列顺序返回；chat_stream 走 BaseLLM 默认实现（单块输出）。"""

    provider_name = "fake"

    def __init__(self, responses: List[str], model: str = "fake-model"):
        super().__init__(model=model)
        self._responses = list(responses)
        self._idx = 0

    async def chat(self, messages, **kwargs):
        i = min(self._idx, len(self._responses) - 1)
        content = self._responses[i]
        self._idx += 1
        usage = TokenUsage(input_tokens=5, output_tokens=10)
        await self._record_usage(usage)
        return LLMResponse(content=content, usage=usage, model=self.model, provider=self.provider_name)


class FakeRetriever(BaseRetriever):
    source_name = "fake"

    def __init__(self, results: List[SearchResult]):
        self._results = list(results)

    async def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        return list(self._results[:max_results])


SAMPLE_SUB_QUESTIONS = ["q1 about X", "q2 about Y", "q3 about Z"]

# fake smart 一次性输出整段；冒头段不带 markdown 标题，正好覆盖"无标题"分支
SAMPLE_REPORT = (
    "这是开头总览：本次调研聚焦三个子方向，发现主流趋势是 X。\n"
    "\n"
    "## 分点综合\n"
    "1. 子问题 A：…\n"
    "2. 子问题 B：…\n"
    "3. 子问题 C：…\n"
    "\n"
    "## 结论\n"
    "综合来看，主流方向是 X。"
)


def _install_fakes(fast_responses, smart_responses, results):
    """patch get_llm + ResearchAgent._retrievers_for_mode；返回 restore 函数。"""
    fast = FakeLLM(fast_responses)
    smart = FakeLLM(smart_responses)

    original_get_llm = research_module.get_llm

    def fake_get_llm(tier=None, config=None):
        return smart if tier == "smart" else fast

    research_module.get_llm = fake_get_llm

    original_retrievers_method = ResearchAgent._retrievers_for_mode
    ResearchAgent._retrievers_for_mode = lambda self, mode: [FakeRetriever(results)]

    def restore():
        research_module.get_llm = original_get_llm
        ResearchAgent._retrievers_for_mode = original_retrievers_method

    return restore


def _researcher_registry() -> AgentRegistry:
    return AgentRegistry([
        AgentSpec(
            name="researcher",
            description="d",
            input_contract="i",
            output_contract="o",
            factory=lambda config=None, websocket=None: ResearchAgent(
                config=config, websocket=websocket
            ),
        ),
    ])


async def case_ok():
    """正常 survey：3 个子问题各检索到 2 条 → 总结 → 出报告 → status=ok。"""
    results = [
        SearchResult(title=f"Paper {i}", url=f"http://x/{i}", snippet="abs", source="fake")
        for i in range(2)
    ]
    fast = [json.dumps(SAMPLE_SUB_QUESTIONS)] + ["子问题摘要"] * 3
    restore = _install_fakes(fast, [SAMPLE_REPORT], results)
    try:
        obs = await DispatchAgentTool(_researcher_registry()).execute(
            agent="researcher", prompt="调研 RL 最新进展", context="聚焦 model-based"
        )
    finally:
        restore()

    assert "[researcher] status=ok" in obs, obs
    head = obs.split("---", 1)[0]
    assert "summary: 这是开头总览" in head, f"summary 应为报告冒头段：\n{head}"
    assert "分点综合" not in head, "summary 不该越界到下一段"
    assert "\n---\nreport:\n" in obs, "observation 应含 report 段"
    body = obs.split("\nreport:\n", 1)[1]
    assert body.strip() == SAMPLE_REPORT.strip(), "report 段应是完整报告原文"
    print("  case_ok OK")


async def case_degenerate():
    """所有子问题都空检索 → status=degenerate + 标准 summary。"""
    fast = [json.dumps(SAMPLE_SUB_QUESTIONS)]  # 空检索路径下 summarize 不再调用
    restore = _install_fakes(fast, ["（基于空检索的占位报告）"], results=[])
    try:
        obs = await DispatchAgentTool(_researcher_registry()).execute(
            agent="researcher", prompt="冷门话题", context=""
        )
    finally:
        restore()
    assert "[researcher] status=degenerate" in obs, obs
    assert "summary: （未检索到相关结果）" in obs, obs
    # degenerate 也带 report（让 hub 自己决定弃用——决策 10.2：不在 dispatch 层挑挑拣拣）
    assert "\n---\nreport:\n" in obs, "degenerate 也应带回 report 段"
    print("  case_degenerate OK")


async def case_context_kwarg():
    """新 context 与旧 background_context kwarg 都触发"携带上轮报告"日志。"""
    results = [SearchResult(title="P", url="u", snippet="s", source="fake")]
    for label, kwargs in [
        ("context (new)", {"context": "上一轮：xxx"}),
        ("background_context (legacy)", {"background_context": "上一轮：xxx"}),
    ]:
        restore = _install_fakes(
            [json.dumps(SAMPLE_SUB_QUESTIONS), "s", "s", "s"], [SAMPLE_REPORT], results
        )
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


async def main() -> None:
    await case_ok()
    await case_degenerate()
    await case_context_kwarg()
    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
