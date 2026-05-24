"""
Step D 验证：WikiAgent ReAct 循环。

离线（默认，无 API）：用脚本化 FakeLLM 确定性地验证循环逻辑——
  - 工具分发 + observation 回填 + 消息往返（assistant 工具轮 / tool 结果轮）
  - touched_files 跟踪、自然停止取 LLM 末轮 content 作 result
  - 兜圈子 nudge 注入一次 + MAX_STEPS 触顶强制结束

在线（加 --live，需 DEEPSEEK_API_KEY）：真实 DeepSeek 端到端冒烟一份文档归档。

跑法：
  PY=/usr/local/Caskroom/miniforge/base/envs/claude-deepseek/bin/python
  $PY tests/check_wiki_agent.py            # 离线
  $PY tests/check_wiki_agent.py --live     # 加真实 DeepSeek 冒烟
"""

from __future__ import annotations

import asyncio
import itertools
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents.wiki_agent as wiki_mod
from agents.wiki_agent import MAX_STEPS, NUDGE_TEXT, WikiAgent
from core.llm.base import BaseLLM, LLMResponse, ToolCall, TokenUsage

_ids = itertools.count()
_REAL_GET_LLM = wiki_mod.get_llm   # 保存真实工厂，--live 场景前还原


def tc(name: str, **args) -> ToolCall:
    return ToolCall(id=f"c{next(_ids)}", name=name, arguments=args)


def tool_resp(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(content="", usage=TokenUsage(), model="fake", provider="fake",
                       tool_calls=list(calls), stop_reason="tool_calls")


def text_resp(text: str) -> LLMResponse:
    return LLMResponse(content=text, usage=TokenUsage(), model="fake", provider="fake",
                       tool_calls=[], stop_reason="stop")


class FakeLLM(BaseLLM):
    """按脚本逐轮返回；脚本耗尽后重复最后一条（用于 MAX_STEPS 测试）。"""

    provider_name = "fake"

    def __init__(self, script: list[LLMResponse]):
        super().__init__(model="fake", api_key=None)
        self.script = script
        self.turns: list[list] = []   # 每轮收到的 messages 快照

    async def chat(self, messages, *, temperature=None, max_tokens=None, tools=None, **kwargs):
        self.turns.append(list(messages))
        resp = self.script[min(len(self.turns) - 1, len(self.script) - 1)]
        await self._record_usage(TokenUsage(input_tokens=1, output_tokens=1))
        return resp


async def collect(agent: WikiAgent, **kwargs) -> list:
    return [ev async for ev in agent.run(kwargs.pop("task", ""), **kwargs)]


def patch_llm(fake: FakeLLM):
    wiki_mod.get_llm = lambda *a, **k: fake  # type: ignore


async def scenario_natural(check) -> None:
    print("\n[场景1] 自然停止（Opt2 流程）：注入 catalog → list → 写页 → 系统重生成 index → 收尾")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "transformer_note.md"
        src.write_text("# Transformer\n注意力机制是核心。", encoding="utf-8")
        wiki = Path(tmp) / "wiki"

        page = ("---\ntitle: Transformer\ncategory: AI\n"
                "description: 注意力机制综述\nentities: [Attention, Transformer]\n"
                "---\n## 知识内容\n注意力机制")
        fake = FakeLLM([
            tool_resp(tc("list_files")),
            tool_resp(tc("write_file", path="AI/transformer.md", content=page)),
            text_resp("完成：新建 AI/transformer.md。"),
        ])
        patch_llm(fake)

        events = await collect(WikiAgent(config=None), files=[str(src)], wiki_root=str(wiki))
        types = [e.type for e in events]
        result = next((e.content for e in events if e.type == "result"), None)

        check((wiki / "AI" / "transformer.md").is_file(), "write_file 真实落盘了页面")
        check("注意力机制" in (wiki / "AI" / "transformer.md").read_text(encoding="utf-8"), "页面内容正确")
        check(result == "完成：新建 AI/transformer.md。", f"自然停止取 LLM 末轮 content 作 result：{result!r}")
        check("result" in types and types.count("result") == 1, "恰好一个 result 事件")
        check(any(e.type == "tokens" for e in events), "有 tokens 事件")
        # 第 1 轮 user 消息注入了 catalog（LLM 无需读 index.md）
        first_user = next(m for m in fake.turns[0] if m.role == "user")
        check("wiki 当前目录" in first_user.content, "开局 prompt 注入了 catalog")
        # index.md 由代码从 frontmatter 重生成（含描述），非 LLM 所写
        idx = (wiki / "index.md").read_text(encoding="utf-8")
        check("[Transformer](AI/transformer.md)" in idx and "注意力机制综述" in idx,
              "index.md 被系统从 frontmatter 重生成（含 description）")
        check(any(e.type == "log" and "重新生成" in e.content for e in events), "有 index 重生成日志")
        # 消息往返：第2轮起 messages 里应出现 assistant 工具轮 + tool 结果轮
        roles_turn2 = [m.role for m in fake.turns[1]]
        check("assistant" in roles_turn2 and "tool" in roles_turn2, f"工具结果回填到消息历史：{roles_turn2}")
        asst = next(m for m in fake.turns[1] if m.role == "assistant")
        check(bool(asst.tool_calls), "assistant 轮携带 tool_calls 回传")


async def scenario_index_blocked(check) -> None:
    print("\n[场景4] index.md 硬挡：LLM 写 index 被拒，但系统仍从 frontmatter 重生成")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "note.md"
        src.write_text("# RAG\n检索增强生成。", encoding="utf-8")
        wiki = Path(tmp) / "wiki"

        page = ("---\ntitle: RAG\ncategory: AI\ndescription: 检索增强生成\n"
                "entities: [RAG]\n---\n## 知识内容\nRAG")
        fake = FakeLLM([
            tool_resp(tc("write_file", path="AI/rag.md", content=page)),
            tool_resp(tc("write_file", path="index.md", content="LLM 乱写的 index")),
            text_resp("完成。"),
        ])
        patch_llm(fake)

        events = await collect(WikiAgent(config=None), files=[str(src)], wiki_root=str(wiki))
        # 写 index.md 的那次工具结果应是 Error（被硬挡）
        leaf_after_index = [
            e.content for e in events
            if e.type == "log" and e.metadata.get("trace") == "leaf" and e.content.startswith("✗")
        ]
        check(any("index.md" in c for c in leaf_after_index), f"写 index.md 被拒（observation 报错）：{leaf_after_index}")
        idx = (wiki / "index.md").read_text(encoding="utf-8")
        check("LLM 乱写的 index" not in idx, "LLM 的 index 写入未生效")
        check("[RAG](AI/rag.md)" in idx, "index.md 仍由系统从 frontmatter 正确重生成")


async def scenario_nudge_and_maxsteps(check) -> None:
    print("\n[场景2] 兜圈子：永远重复同一调用 → nudge 一次 + MAX_STEPS 触顶")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "doc.md"
        src.write_text("内容", encoding="utf-8")
        wiki = Path(tmp) / "wiki"

        fake = FakeLLM([tool_resp(tc("list_files"))])  # 脚本耗尽后一直重复 list_files
        patch_llm(fake)

        events = await collect(WikiAgent(config=None), files=[str(src)], wiki_root=str(wiki))
        nudge_logs = [e for e in events if e.type == "log" and "nudge" in e.content]
        result = next((e.content for e in events if e.type == "result"), None)

        check(len(fake.turns) == MAX_STEPS, f"LLM 被调用 MAX_STEPS={MAX_STEPS} 次（实际 {len(fake.turns)}）")
        check(len(nudge_logs) == 1, f"nudge 恰好注入一次（实际 {len(nudge_logs)}）")
        # nudge 文本确实进了后续轮的 messages
        nudged_in_msgs = any(
            any(m.content == NUDGE_TEXT for m in turn) for turn in fake.turns
        )
        check(nudged_in_msgs, "nudge 文本进入了消息历史")
        check(result is not None and "步数上限" in result, f"result 含触顶说明：{result!r}")
        check(any(e.type == "log" and "步数上限" in e.content for e in events), "有触顶 warning 日志")


async def scenario_no_input(check) -> None:
    print("\n[场景3] 无可读输入文档 → 抛 ValueError")
    fake = FakeLLM([text_resp("x")])
    patch_llm(fake)
    raised = False
    try:
        await collect(WikiAgent(config=None), files=["/nonexistent/nope.md"])
    except ValueError:
        raised = True
    check(raised, "无可读文档时 run() 抛 ValueError（交 runner 兜）")


async def scenario_live(check) -> None:
    print("\n[场景5 --live] 真实 DeepSeek 端到端冒烟")
    wiki_mod.get_llm = _REAL_GET_LLM   # 还原被前面场景 monkeypatch 掉的工厂
    from core.config import Config
    src_text = (
        "# Transformer 架构\n\n"
        "Transformer 由 Vaswani 等人在 2017 年提出，核心是自注意力（self-attention）机制，"
        "摒弃了 RNN 的循环结构，支持并行训练。多头注意力（multi-head attention）让模型"
        "在不同子空间关注不同信息。位置编码（positional encoding）注入序列顺序信息。"
    )
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "transformer.md"
        src.write_text(src_text, encoding="utf-8")
        wiki = Path(tmp) / "wiki"

        agent = WikiAgent(config=Config.from_env())
        events = await collect(agent, files=[str(src)], wiki_root=str(wiki))
        for e in events:
            if e.type in ("log", "result"):
                tag = "结果" if e.type == "result" else "日志"
                print(f"  [{tag}] {e.content[:120]}")

        result = next((e.content for e in events if e.type == "result"), None)
        pages = [p for p in wiki.rglob("*.md") if p.name != "index.md"]
        index_txt = (wiki / "index.md").read_text(encoding="utf-8")

        check(result is not None and len(result) > 0, "有非空 result")
        check(len(pages) >= 1, f"至少新建了一个知识页面：{[str(p.relative_to(wiki)) for p in pages]}")
        check("尚未有内容" not in index_txt, "index.md 已从骨架被更新")


async def main() -> int:
    live = "--live" in sys.argv
    failures: list[str] = []

    def check(cond: bool, msg: str):
        print(f"  {'✓' if cond else '✗'} {msg}")
        if not cond:
            failures.append(msg)

    await scenario_natural(check)
    await scenario_nudge_and_maxsteps(check)
    await scenario_index_blocked(check)
    await scenario_no_input(check)
    if live:
        await scenario_live(check)
    else:
        print("\n(跳过 --live 真实 DeepSeek 冒烟；加 --live 开启)")

    print()
    if failures:
        print(f"❌ {len(failures)} 项失败")
        return 1
    print("✅ WikiAgent 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
