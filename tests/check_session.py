"""
Step 1 验证脚本 —— core/session.py 的独立闭环测试。

无 LLM / 无网络 / 无 pytest 依赖，直接跑：
    $PY tests/check_session.py
全部断言通过则打印 OK 并以退出码 0 结束。
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.session import RECENT_TURNS, SessionManager  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sm = SessionManager(reports_dir=tmp, window_size=2)
        sid = "test-session"

        # 1) 加 4 份报告：最老 2 份 content=None，最新 2 份保有 content
        records = []
        for i in range(1, 5):
            rec = sm.save_report(
                sid, topic=f"主题{i}", description=f"摘要{i}", content=f"正文内容 {i}"
            )
            records.append(rec)

        session = sm.get_or_create(sid)
        assert len(session.reports) == 4, "应保留 4 条报告元数据"
        assert session.reports[0].content is None, "最老报告 content 应被置 None"
        assert session.reports[1].content is None, "次老报告 content 应被置 None"
        assert session.reports[2].content == "正文内容 3", "窗口内报告应保有正文"
        assert session.reports[3].content == "正文内容 4", "窗口内报告应保有正文"

        # 2) 4 个 .md 文件都在磁盘，内容正确（即使内存 content 已置 None）
        for i, rec in enumerate(records, 1):
            p = Path(rec.file_path)
            assert p.exists(), f"报告文件应存在：{p}"
            assert p.read_text(encoding="utf-8") == f"正文内容 {i}", "落盘正文应完整"

        # 3) get_recent_context：报告只含窗口内有 content 的；Turn 数 ≤ RECENT_TURNS
        for n in range(RECENT_TURNS + 3):
            sm.add_turn(sid, f"问题{n}", f"回答{n}", route="research", mode="survey")
        ctx = sm.get_recent_context(sid)
        assert len(ctx.reports) == 2, "上下文应只含窗口内的 2 份报告"
        assert all(r.content is not None for r in ctx.reports), "返回报告应都有正文"
        assert len(ctx.turns) == RECENT_TURNS, f"应只返回最近 {RECENT_TURNS} 轮"
        assert ctx.turns[-1].user_input == f"问题{RECENT_TURNS + 2}", "应是最新一轮在末尾"

        # 4) get_or_create：同 id 同对象，不同 id 隔离
        assert sm.get_or_create(sid) is session, "同 id 应返回同一 Session 对象"
        other = sm.get_or_create("another")
        assert other is not session and other.reports == [], "不同 id 应互相隔离"

    print("OK")


if __name__ == "__main__":
    main()
