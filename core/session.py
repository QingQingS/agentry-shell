"""
会话记忆 —— 阶段三连续对话的状态层。

职责（纯数据 + 持久化，零 LLM / 零 Agent 依赖）：
    - 在内存中维护多个 Session（按 session_id 隔离）
    - 报告正文落盘到 ./reports/{session_id}/{ts}_{slug}.md
    - 窗口管理：仅最近 WINDOW_SIZE 份报告在内存保有正文，更老的 content 置 None
      （正文已在磁盘，file_path 供未来按需加载）

边界约定：
    - description（报告摘要）由调用方传入，不在此生成（摘要的 LLM 调用属于 Orchestrator）
    - route / mode 以纯字符串存储，不依赖 core.intent
    - Session 元数据只在内存；仅报告正文持久化为文件。进程重启后 session 丢失、文件仍在
      （与「WS 重连 = 新 session」一致；JSON/sqlite 持久化属于后续阶段）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

WINDOW_SIZE = 2          # 内存中保有正文的最近报告份数
RECENT_TURNS = 5         # get_recent_context 返回的最近对话轮数上限


@dataclass
class ReportRecord:
    topic: str
    description: str              # 1-2 句摘要（调用方传入）
    file_path: str                # 正文落盘路径
    content: Optional[str]        # 窗口内有值；超窗置 None（正文留在磁盘）
    timestamp: str                # ISO 时间


@dataclass
class Turn:
    user_input: str
    agent_response: str
    route: str                    # "research" / "chat"，纯字符串
    mode: str                     # "survey" / ...；chat 时为空串
    timestamp: str


@dataclass
class Session:
    session_id: str
    turns: List[Turn] = field(default_factory=list)
    reports: List[ReportRecord] = field(default_factory=list)


@dataclass
class RecentContext:
    """get_recent_context 的结构化返回；prompt 拼接由消费方负责。"""
    reports: List[ReportRecord]   # 仅窗口内仍有 content 的报告（最新在后）
    turns: List[Turn]             # 最近 RECENT_TURNS 轮（最新在后）


def _slugify(topic: str, max_len: int = 40) -> str:
    """把话题转成文件名安全的 slug；保留 unicode 词字符（含中文）。"""
    slug = re.sub(r"[^\w\-]+", "-", topic.strip(), flags=re.UNICODE).strip("-")
    return (slug[:max_len].rstrip("-")) or "report"


class SessionManager:
    """
    多 Session 的内存存储 + 报告正文落盘 + 窗口管理。

    典型用法（Orchestrator）：
        sm = SessionManager()
        session = sm.get_or_create(session_id)
        sm.save_report(session_id, topic, description, content)
        sm.add_turn(session_id, user_input, response, route, mode)
        ctx = sm.get_recent_context(session_id)
    """

    def __init__(self, reports_dir: str = "reports", window_size: int = WINDOW_SIZE):
        self._sessions: Dict[str, Session] = {}
        self._reports_dir = Path(reports_dir)
        self._window_size = window_size

    def get_or_create(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            session = Session(session_id=session_id)
            self._sessions[session_id] = session
        return session

    def save_report(
        self, session_id: str, topic: str, description: str, content: str
    ) -> ReportRecord:
        """落盘正文 → 追加记录 → 应用窗口（超窗的旧报告 content 置 None）。"""
        session = self.get_or_create(session_id)
        now = datetime.now()
        ts_file = now.strftime("%Y%m%d_%H%M%S_%f")

        report_dir = self._reports_dir / session_id
        report_dir.mkdir(parents=True, exist_ok=True)
        file_path = report_dir / f"{ts_file}_{_slugify(topic)}.md"
        file_path.write_text(content, encoding="utf-8")

        record = ReportRecord(
            topic=topic,
            description=description,
            file_path=str(file_path),
            content=content,
            timestamp=now.isoformat(),
        )
        session.reports.append(record)
        self._apply_window(session)
        return record

    def add_turn(
        self,
        session_id: str,
        user_input: str,
        agent_response: str,
        route: str,
        mode: str = "",
    ) -> Turn:
        session = self.get_or_create(session_id)
        turn = Turn(
            user_input=user_input,
            agent_response=agent_response,
            route=route,
            mode=mode,
            timestamp=datetime.now().isoformat(),
        )
        session.turns.append(turn)
        return turn

    def get_recent_context(self, session_id: str) -> RecentContext:
        """返回结构化上下文：窗口内仍有正文的报告 + 最近若干轮对话。"""
        session = self.get_or_create(session_id)
        live_reports = [r for r in session.reports if r.content is not None]
        recent_turns = session.turns[-RECENT_TURNS:]
        return RecentContext(reports=live_reports, turns=recent_turns)

    def _apply_window(self, session: Session) -> None:
        """除最近 window_size 份外，其余报告的 content 置 None（正文已落盘）。"""
        if self._window_size <= 0:
            cutoff = session.reports
        else:
            cutoff = session.reports[: -self._window_size]
        for record in cutoff:
            record.content = None
