"""
ResearchAgent._make_retrievers + ChatAgent context 注入的离线断言。

步5.5 后 ResearchAgent 内部 ReAct 化，原本测的 _normalize_mode / _retrievers_for_mode /
_focused_* / ResearchMode 都已下线（迁进 agents/research_tools.py 内部或被删）；
ReAct 循环和工具表的覆盖移到 tests/check_research_spoke.py。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TAVILY_API_KEY", "test-key")  # TavilyRetriever 构造需要 key

from agents.chat_agent import ChatAgent  # noqa: E402
from agents.research_agent import ResearchAgent  # noqa: E402


def main() -> None:
    # 1) ResearchAgent._make_retrievers 解析 config.retriever（逗号分隔多源）。
    class Cfg:
        retriever = "arxiv"
        tavily_api_key = "k"

    agent = ResearchAgent(config=Cfg())
    rs = agent._make_retrievers()
    assert [r.source_name for r in rs] == ["arxiv"], rs

    class Cfg2:
        retriever = "arxiv,tavily"
        tavily_api_key = "k"

    agent2 = ResearchAgent(config=Cfg2())
    rs2 = agent2._make_retrievers()
    assert [r.source_name for r in rs2] == ["arxiv", "tavily"], rs2

    # config=None / 未配 retriever → 默认 arxiv 兜底
    agent3 = ResearchAgent(config=None)
    rs3 = agent3._make_retrievers()
    assert [r.source_name for r in rs3] == ["arxiv"], rs3

    # 2) ChatAgent context 注入 system prompt
    chat = ChatAgent(config=None)
    assert "BG_CTX" in chat._system_with_context("BG_CTX")
    assert chat.SYSTEM_PROMPT in chat._system_with_context("BG_CTX")
    assert chat._system_with_context("") == chat.SYSTEM_PROMPT, "无 context 时等于原 prompt"

    print("OK")


if __name__ == "__main__":
    main()
