"""
Step 3 验证脚本 —— ResearchAgent mode 分支 + ChatAgent context 注入的离线断言。

不发网络请求（只校验检索器选择、prompt 构造、context 拼接等纯逻辑）：
    $PY tests/check_agents.py
全部断言通过则打印 OK。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# code_search 会构造 TavilyRetriever（需要 key 才能实例化）；设 dummy key，不会真的发请求。
os.environ.setdefault("TAVILY_API_KEY", "test-key")

from agents.chat_agent import ChatAgent  # noqa: E402
from agents.research_agent import ResearchAgent, ResearchMode  # noqa: E402


def _texts(messages) -> str:
    return "\n".join(m.content for m in messages)


def main() -> None:
    agent = ResearchAgent(config=None)

    # 1) mode 归一化：非法值降级为 survey
    assert agent._normalize_mode("survey") == ResearchMode.SURVEY
    assert agent._normalize_mode("bogus") == ResearchMode.SURVEY
    assert agent._normalize_mode(None) == ResearchMode.SURVEY

    # 2) 每个 mode 选对检索器
    survey_rs = agent._retrievers_for_mode(ResearchMode.SURVEY)
    assert [r.source_name for r in survey_rs] == ["arxiv"], "config=None → survey 默认 arxiv"
    paper_rs = agent._retrievers_for_mode(ResearchMode.PAPER_LOOKUP)
    assert [r.source_name for r in paper_rs] == ["arxiv"], "paper_lookup 固定 arxiv"
    code_rs = agent._retrievers_for_mode(ResearchMode.CODE_SEARCH)
    assert [r.source_name for r in code_rs] == ["tavily"], "code_search 固定 tavily"

    # 3) focused query：code_search 增广 github 关键词，paper_lookup 原样
    assert agent._focused_query("Attention Is All You Need", ResearchMode.PAPER_LOOKUP) == "Attention Is All You Need"
    assert "github" in agent._focused_query("Attention Is All You Need", ResearchMode.CODE_SEARCH).lower()

    # 4) background_context 注入 survey 的拆解 + 报告 prompt
    BG = "BACKGROUND_MARKER_X"
    assert BG in _texts(agent._decompose_messages("t", BG))
    assert BG not in _texts(agent._decompose_messages("t", "")), "无背景时不应出现背景标记"
    assert BG in _texts(agent._report_messages("t", [("q", "s")], BG))
    assert BG not in _texts(agent._report_messages("t", [("q", "s")], ""))

    # 5) focused 报告 prompt：mode 区分 + 背景注入
    from core.retrievers import SearchResult
    res = [SearchResult(title="T", url="http://x", snippet="s", source="arxiv")]
    paper_msgs = agent._focused_report_messages("t", res, BG, ResearchMode.PAPER_LOOKUP)
    code_msgs = agent._focused_report_messages("t", res, BG, ResearchMode.CODE_SEARCH)
    assert "综述" in _texts(paper_msgs), "paper_lookup 应是综述型 prompt"
    assert "GitHub" in _texts(code_msgs) or "开源" in _texts(code_msgs), "code_search 应是找仓库型 prompt"
    assert BG in _texts(paper_msgs) and BG in _texts(code_msgs)

    # 6) ChatAgent context 注入 system prompt
    chat = ChatAgent(config=None)
    assert "BG_CTX" in chat._system_with_context("BG_CTX")
    assert chat.SYSTEM_PROMPT in chat._system_with_context("BG_CTX")
    assert chat._system_with_context("") == chat.SYSTEM_PROMPT, "无 context 时等于原 prompt"

    print("OK")


if __name__ == "__main__":
    main()
