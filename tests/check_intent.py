"""
Step 2 验证脚本 —— core/intent.py 的分类逻辑。

默认离线运行（用假 LLM 校验跳过/降级/解析三条路径）：
    $PY tests/check_intent.py
加 --live 调真实 DeepSeek 跑 4 场景打印结果供人工确认（需 .env 的 DEEPSEEK_API_KEY）：
    $PY tests/check_intent.py --live
离线断言全部通过则打印 OK。
"""

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.intent import classify_intent  # noqa: E402
from core.session import RecentContext, ReportRecord, Turn  # noqa: E402


@dataclass
class _FakeResp:
    content: str


class StubLLM:
    """返回预设字符串；记录是否被调用，用于断言「无上下文跳过 LLM」。"""

    def __init__(self, content: str = ""):
        self.content = content
        self.called = False

    async def chat(self, messages):
        self.called = True
        return _FakeResp(self.content)


def _ctx() -> RecentContext:
    return RecentContext(
        reports=[ReportRecord("Transformer", "讲注意力机制的报告", "/p.md", "正文", "2026-05-22T00:00:00")],
        turns=[Turn("研究 Transformer", "已生成报告", "research", "survey", "2026-05-22T00:00:00")],
    )


async def offline_checks() -> None:
    # 1) 无上下文 → 跳过 LLM，降级 research/survey，target=输入
    llm = StubLLM(content="should not be used")
    r = await classify_intent("研究大模型", None, llm)
    assert not llm.called, "无上下文时不应调用 LLM"
    assert (r.route, r.mode, r.carry_context) == ("research", "survey", False)
    assert r.target == "研究大模型"

    # 2) 空上下文（无报告无对话）同样跳过
    llm = StubLLM(content="x")
    r = await classify_intent("hi", RecentContext(reports=[], turns=[]), llm)
    assert not llm.called and r.route == "research"

    # 3) 坏 JSON → 降级
    llm = StubLLM(content="这不是 JSON")
    r = await classify_intent("继续", _ctx(), llm)
    assert llm.called, "有上下文应调用 LLM"
    assert (r.route, r.mode, r.carry_context) == ("research", "survey", False)
    assert r.target == "继续", "降级时 target 回退为原始输入"

    # 4) 合法 JSON → 正确解析
    llm = StubLLM(content='{"route":"chat","mode":"survey","target":"","carry_context":true}')
    r = await classify_intent("刚才说的注意力是什么", _ctx(), llm)
    assert (r.route, r.mode, r.carry_context) == ("chat", "survey", True)

    # 5) JSON 里非法 mode → mode 兜底 survey，其余保留
    llm = StubLLM(content='{"route":"research","mode":"weird","target":"x","carry_context":false}')
    r = await classify_intent("找代码", _ctx(), llm)
    assert r.route == "research" and r.mode == "survey" and r.target == "x"

    print("OK")


async def live_checks() -> None:
    from core.config import Config
    from core.llm import get_llm

    llm = get_llm(tier="fast", config=Config())
    ctx = _ctx()
    cases = [
        "2026 年大模型最新进展",
        "讲讲 Attention Is All You Need 这篇论文",
        "帮我找找它的开源实现",
        "刚才报告里说的注意力机制是什么意思",
    ]
    for q in cases:
        r = await classify_intent(q, ctx, llm)
        print(f"[{q}]\n  → route={r.route} mode={r.mode} carry={r.carry_context} target={r.target!r}\n")


def main() -> None:
    asyncio.run(offline_checks())
    if "--live" in sys.argv:
        print("--- live (人工确认) ---")
        asyncio.run(live_checks())


if __name__ == "__main__":
    main()
