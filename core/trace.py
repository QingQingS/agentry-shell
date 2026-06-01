"""
LLM 调用的无损增量追踪 —— 把每次 chat() 的完整输入增量与完整响应原样落盘。

为什么存在（问题）：
    ReAct 的真实过程从未被完整保留——唯一的出口是为「显示」而生的有损事件流
    （只 yield 思考片段 / 工具名 / 字数），而那份无损的 ground truth（完整 system
    prompt + LLM 原样输出含 tool_call 全参数 + 工具结果原文）一直只活在某次 run()
    的 messages 数组里，函数返回即销毁。本模块给它开一个独立于「显示」的「存档」出口。

设计（方案 1 · 步 1）：
    - tap 在 BaseLLM.chat（所有 provider 之下、所有 agent 之上）→ 新 agent / 新
      provider 都自动被记，agent 自己零改动。
    - 记「增量」而非「累加全量」：chat() 每次收到的是不断变长的完整对话，若每次落全量
      会层层重复。故每次只落「输入尾巴里的非 assistant 消息」（seed / 工具结果原文 /
      nudge 注入），assistant 答复另由 response 记录单独落 —— 二者不重不漏。
        · 第 k 次返回的 assistant 答复，会在第 k+1 次作为输入尾巴再次出现 →
          按 role==assistant 跳过，避免重复（它已由 response 记录承载）。
        · 末轮答复没有「下一次输入」，但已由 response 记录落下 → 不遗漏。
      按 seq 顺序把 msg + response 记录串起来，即可无损重建结束时的 messages 数组。
    - 前提：当前 agent 循环对 messages「只增不改」。裁剪 / 压缩（README −72%）属
      未来能力，到时由步 4 的 append-only 守卫把「历史被改写」也转成事件，此处不预设。
    - 落盘：JSONL，一次进程 run 一个文件 <TRACE_DIR>/run-<ts>-<pid>.jsonl。
    - 默认开；TRACE=0 关，TRACE_DIR 改目录（默认 ./traces）。
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

# 本模块刻意不 import core.llm 的任何类型（避免循环依赖）：对 message / response
# 一律鸭子类型读取属性，序列化即可。


# ---- 序列化（原样，不裁剪）---------------------------------------------------

def serialize_message(m: Any) -> dict:
    """ChatMessage → 可落盘 dict，保留全部字段（含 tool_calls 完整参数）。"""
    d: dict = {"role": m.role, "content": m.content}
    if getattr(m, "tool_calls", None):
        d["tool_calls"] = [_serialize_tool_call(tc) for tc in m.tool_calls]
    if getattr(m, "tool_call_id", None) is not None:
        d["tool_call_id"] = m.tool_call_id
    if getattr(m, "reasoning_content", None) is not None:
        d["reasoning_content"] = m.reasoning_content
    return d


def serialize_response(resp: Any) -> dict:
    """LLMResponse → 可落盘 dict，保留 content / tool_calls 全参数 / reasoning。"""
    d: dict = {"content": resp.content, "stop_reason": getattr(resp, "stop_reason", "stop")}
    if getattr(resp, "tool_calls", None):
        d["tool_calls"] = [_serialize_tool_call(tc) for tc in resp.tool_calls]
    if getattr(resp, "reasoning_content", None) is not None:
        d["reasoning_content"] = resp.reasoning_content
    return d


def _serialize_tool_call(tc: Any) -> dict:
    return {"id": tc.id, "name": tc.name, "arguments": tc.arguments}


def _fingerprint(m: Any) -> str:
    """消息的稳定指纹，用于探测「已记录的前缀是否被改写/截断」（步4 守卫）。"""
    blob = json.dumps(serialize_message(m), sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


# ---- run 作用域（跨 agent 层级）---------------------------------------------
# 每次 agent 运行（core.runner.run_agent）开一个 run：mint run_id，parent 取当前 run。
# hub 入口和 dispatch 派发的 spoke 都经 run_agent，且 spoke 的 run_agent 跑在
# asyncio.gather 复制出的隔离 context 里 → 父子归属天然成立、并发兄弟互不串味。
# 每个 LLM 记录据此带上 run_id / parent_run_id / agent，replay 即可拼成树。

@dataclass
class _RunCtx:
    run_id: str
    parent_run_id: Optional[str]
    agent: Optional[str]


_current_run: contextvars.ContextVar[Optional[_RunCtx]] = contextvars.ContextVar(
    "trace_current_run", default=None
)


def enter_run(agent: Optional[str] = None) -> contextvars.Token:
    """开一个 run 作用域，返回 token（交给 exit_run 还原）。"""
    parent = _current_run.get()
    ctx = _RunCtx(
        run_id=uuid.uuid4().hex[:8],
        parent_run_id=parent.run_id if parent is not None else None,
        agent=agent,
    )
    return _current_run.set(ctx)


def exit_run(token: contextvars.Token) -> None:
    """还原到父作用域；token 跨 context 失效时静默忽略（异步生成器提前关闭等边界）。"""
    try:
        _current_run.reset(token)
    except (ValueError, LookupError):
        pass


def current_run() -> Optional[_RunCtx]:
    return _current_run.get()


# ---- 进程级 sink ------------------------------------------------------------

class _Sink:
    """进程内单例写口：惰性读 env、一次 run 开一个 JSONL 文件、逐行 flush。"""

    def __init__(self) -> None:
        self._fh = None
        self._path: Optional[Path] = None
        self._dir: Optional[Path] = None
        self._enabled: Optional[bool] = None   # None=未决（首次 emit 时读 env）

    def configure(self, *, dir: Optional[str] = None, enabled: bool = True) -> None:
        """显式配置（主要供测试）：关掉旧文件、改目录、下次 emit 重新开新文件。"""
        self._close()
        self._enabled = enabled
        if dir is not None:
            self._dir = Path(dir)
        self._path = None

    def reset(self) -> None:
        """关文件并清空状态（测试间隔离）。"""
        self._close()
        self._path = None
        self._dir = None
        self._enabled = None

    def _resolve(self) -> None:
        if self._enabled is None:
            self._enabled = os.getenv("TRACE", "1") != "0"
        if self._dir is None:
            self._dir = Path(os.getenv("TRACE_DIR", "traces"))

    @property
    def enabled(self) -> bool:
        self._resolve()
        return bool(self._enabled)

    @property
    def path(self) -> Optional[Path]:
        return self._path

    def _ensure_open(self) -> None:
        if self._fh is not None:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        self._path = self._dir / f"run-{ts}-{os.getpid()}.jsonl"
        self._fh = self._path.open("a", encoding="utf-8")

    def emit(self, record: dict) -> None:
        if not self.enabled:
            return
        self._ensure_open()
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()

    def _close(self) -> None:
        if self._fh is not None:
            self._fh.close()
        self._fh = None


_sink = _Sink()


def configure(*, dir: Optional[str] = None, enabled: bool = True) -> None:
    _sink.configure(dir=dir, enabled=enabled)


def reset() -> None:
    _sink.reset()


def current_path() -> Optional[Path]:
    return _sink.path


# ---- per-LLM-instance tracer ------------------------------------------------

class LLMTracer:
    """每个 BaseLLM 实例持有一个，跨该实例的多次 chat() 维护增量高水位 hwm。

    一个对话 = 一个 provider 实例（agent 每次 run 各自 get_llm，互不共享）→
    per-instance 状态即可正确切流；跨 agent 的父子归属留给步 2（contextvars）。
    """

    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model
        self.stream_id = uuid.uuid4().hex[:8]
        self._hwm = 0           # 已记录到 messages 的哪个下标
        self._seq = 0           # 该流内单调序号，供 replay 排序
        self._fps: List[str] = []   # 已记录各位置的指纹，用于探测前缀被改写（步4）

    def on_request(self, messages: List[Any]) -> None:
        """落输入增量。正常（append-only）只落尾巴里的非 assistant 消息
        （seed / 工具结果 / 注入）；assistant 答复由 on_response 承载，避免重复。

        步4 守卫：若已记录的前缀被改写或截断（未来上下文压缩/裁剪），增量假设失效，
        改落一条 context_edited 事件 + 全量 resync 快照——「历史被动过」本身留痕，
        且文件仍能据快照无损重建当前上下文。replay 见到此记录即把基线切到 snapshot。"""
        if not _sink.enabled:
            return
        n = len(messages)
        diverged_at = self._find_divergence(messages, n)
        if diverged_at is not None:
            self._emit("context_edited", {
                "diverged_at": diverged_at,
                "old_len": self._hwm,
                "new_len": n,
                "snapshot": [serialize_message(m) for m in messages],
            })
        else:
            for m in messages[self._hwm:]:
                if m.role == "assistant":
                    continue
                self._emit("msg", serialize_message(m))
        self._fps = [_fingerprint(m) for m in messages]
        self._hwm = n

    def _find_divergence(self, messages: List[Any], n: int) -> Optional[int]:
        """返回前缀首个被改写的下标；纯截断（前缀一致但变短）返回新长度；否则 None。"""
        limit = min(self._hwm, n)
        for i in range(limit):
            if _fingerprint(messages[i]) != self._fps[i]:
                return i
        if n < self._hwm:
            return n
        return None

    def on_response(self, resp: Any, dt: float) -> None:
        """落本次返回的完整响应（assistant 增量）。"""
        if not _sink.enabled:
            return
        usage = getattr(resp, "usage", None)
        self._emit(
            "response",
            serialize_response(resp),
            dt=round(dt, 4),
            usage=usage.to_dict() if usage is not None else None,
        )

    def _emit(self, kind: str, payload: dict, **extra: Any) -> None:
        self._seq += 1
        record = {
            "ts": time.time(),
            "stream_id": self.stream_id,
            "seq": self._seq,
            "provider": self.provider,
            "model": self.model,
            "kind": kind,
            "payload": payload,
        }
        # 跨 agent 层级：标注本条属于哪个 run、其父 run、哪个 agent（步 2）。
        # stream_id 仍区分同一 run 内的多个 LLM 实例（如 researcher 的 smart 主循环
        # 与 fast 的 broad_survey 子调用）。
        run = current_run()
        if run is not None:
            record["run_id"] = run.run_id
            if run.parent_run_id is not None:
                record["parent_run_id"] = run.parent_run_id
            if run.agent is not None:
                record["agent"] = run.agent
        record.update({k: v for k, v in extra.items() if v is not None})
        _sink.emit(record)
