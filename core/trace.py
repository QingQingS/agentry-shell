"""
LLM 调用的无损追踪 —— 把每次 chat() 的完整输入快照与完整响应原样落盘。

为什么存在（问题）：
    ReAct 的真实过程从未被完整保留——唯一的出口是为「显示」而生的有损事件流
    （只 yield 思考片段 / 工具名 / 字数），而那份无损的 ground truth（完整 system
    prompt + LLM 原样输出含 tool_call 全参数 + 工具结果原文）一直只活在某次 run()
    的 messages 数组里，函数返回即销毁。本模块给它开一个独立于「显示」的「存档」出口。

设计（全量快照，写读分离）：
    - tap 在 BaseLLM.chat（所有 provider 之下、所有 agent 之上）→ 新 agent / 新
      provider 都自动被记，agent 自己零改动。
    - 日志层是**无状态旁观者**：每次 chat 原样落「本次完整 input 快照 + 本次完整
      output」，**完全不理解 input 里面是什么**（不区分历史 / 临时注入 / 是否被裁剪）。
      → 与功能层彻底解耦：agent 怎么构造请求（如末尾搭车的健康度仪表盘）随便变，
        日志层一行都不用改。LLM 调用本身无状态、每步自包含，记录层就镜像这种形态。
    - 不再用「增量 + 跨调用基线」：那套需要假设 messages 只增不改，于是被迫处理
      实例复用 / 临时注入 / 裁剪压缩等一堆破例（旧步4 的指纹守卫即为此而生）。改成
      全量快照后，这些破例统统不构成问题——每条 request 记录独立自包含。
        · 体积代价：单条会话内 request 快照是 O(n²) 冗余（每条含至此的全部历史）。
          应对：response 全留（O(n)、是决策链核心）；request 快照交给 replay
          **读时窗口**（只渲染最近若干个）+ **读时 diff**（相邻快照只显示新增尾巴）。
          即「易变易错的逻辑放可重算的读路径，写路径只管最简最稳地如实落盘」。
        · append-only 会话里，最新一条 request 快照即「全量历史」——replay 取末条
          即可，无需重建。
    - 归属标签（让记录可跟踪、可聚合）：每条记录带
        run_id / parent_run_id / agent（步2：agent 运行层级，hub→spoke 派生树）
        conv_id（会话层：同一条对话线程共享；默认「一个 run 一条会话」）。
      conv_id 是 agent 声明的**身份标签**，日志层只是读环境贴上去——不解析 input，
      故不构成与功能层的耦合。
    - 落盘：JSONL，一次进程 run 一个文件 <TRACE_DIR>/run-<ts>-<pid>.jsonl。
    - 默认开；TRACE=0 关，TRACE_DIR 改目录（默认 ./traces）。
"""

from __future__ import annotations

import contextvars
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


# ---- run 作用域（跨 agent 层级 + 会话归属）---------------------------------
# 每次 agent 运行（core.runner.run_agent）开一个 run：mint run_id，parent 取当前 run。
# hub 入口和 dispatch 派发的 spoke 都经 run_agent，且 spoke 的 run_agent 跑在
# asyncio.gather 复制出的隔离 context 里 → 父子归属天然成立、并发兄弟互不串味。
# 同时 mint conv_id 作为「该 run 的默认会话」——绝大多数 agent「一个 run = 一条
# 对话线程」，默认兜底即可正确聚合；将来「一个 run 内多条会话」（如 broad_survey 借
# 同一实例开多次一次性对话）再引入显式会话作用域覆盖 conv_id。

@dataclass
class _RunCtx:
    run_id: str
    parent_run_id: Optional[str]
    agent: Optional[str]
    conv_id: str


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
        conv_id=uuid.uuid4().hex[:8],
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
    """每个 BaseLLM 实例持有一个：原样落「本次完整 input 快照」与「本次完整 output」。

    无状态旁观者——不跨调用维护任何「对话基线」，故实例被谁复用、input 怎么构造都
    无所谓。stream_id 仅作「这些记录来自哪个 LLM 实例」的元数据；正确的会话聚合靠
    conv_id（默认随 run）。
    """

    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model
        self.stream_id = uuid.uuid4().hex[:8]
        self._seq = 0           # 该实例内单调序号，供 replay 排序

    def on_request(self, messages: List[Any]) -> None:
        """落本次完整输入快照（原样、全量）。不理解 input 内部结构、不维护基线。"""
        if not _sink.enabled:
            return
        self._emit("request", {"snapshot": [serialize_message(m) for m in messages]})

    def on_response(self, resp: Any, dt: float) -> None:
        """落本次返回的完整响应。"""
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
        # 归属标签：本条属于哪个 run、其父 run、哪个 agent（步2）+ 哪条会话（conv_id）。
        # conv_id 默认随 run（一 run 一会话）；无 run 作用域时退回 stream_id（一实例一会话）。
        run = current_run()
        if run is not None:
            record["run_id"] = run.run_id
            record["conv_id"] = run.conv_id
            if run.parent_run_id is not None:
                record["parent_run_id"] = run.parent_run_id
            if run.agent is not None:
                record["agent"] = run.agent
        else:
            record["conv_id"] = self.stream_id
        record.update({k: v for k, v in extra.items() if v is not None})
        _sink.emit(record)
