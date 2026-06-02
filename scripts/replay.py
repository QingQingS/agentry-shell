#!/usr/bin/env python3
"""
replay —— 把一次 run 的 trace（core/trace.py 落的 JSONL）按层级还原成人读的 transcript。

这是「复盘」的最终出口。写端只管原样落「每次 chat 的完整 input 快照 + 完整 output」，
本工具在**读时**把它还原成人能读的过程：
  - 按 parent_run_id 把记录拼成 hub→spoke→子调用的树；
  - 每个 run 内按 conv_id 聚合会话；同一会话按 seq 还原逐轮 transcript；
  - **读时窗口**：每条会话只渲染最近 WINDOW 个 request 快照（更早的省略——append-only
    会话里窗口首个快照即含至此的完整历史，故内容不丢，只是早期的逐步演化不展开）；
  - **读时 diff**：相邻 request 快照只显示「新增的尾巴」（assistant 增量由 response
    记录渲染，故跳过），避免每条全量快照层层重复刷屏。
  - 完整渲染此前永远拿不回的三样：每个 agent 的完整 system prompt、LLM 原样输出的
    tool_call 全参数（含整篇报告）、LLM 实际收到的工具结果原文。

读端「模型先行 + 多目标渲染」：
    build_tree(records) → 结构化节点树（run→conv→item，已做 diff/window）；
    render_text(tree) / render_html(tree) 两个渲染器消费同一份模型 → 终端 transcript
    与 HTML 阅读页**证明性地展示同一份内容**，diff/window 逻辑只有一份、不会漂移。
    render_tree() 保留为 render_text(build_tree(...)) 的薄封装（向后兼容既有调用）。

用法：
    python scripts/replay.py [trace.jsonl]        # 省略则取 TRACE_DIR(默认 traces/) 里最新的
    python scripts/replay.py --max 800            # 长正文截断到 800 字符（默认 0=不截断）
    python scripts/replay.py --window 0           # 关掉读时窗口（每条会话全量展开）
    python scripts/replay.py --html               # 生成 HTML 阅读页（写到 <trace>.html 旁边）
    python scripts/replay.py --html out.html      # 指定 HTML 输出路径
    python scripts/replay.py --html --open        # 生成后用默认浏览器打开
"""

from __future__ import annotations

import argparse
import html
import json
import os
import webbrowser
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

WINDOW = 5   # 读时窗口：每条会话默认只渲染最近这么多个 request 快照（0=不限）


def load_records(path: Path) -> List[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def latest_trace(trace_dir: Path) -> Optional[Path]:
    files = sorted(trace_dir.glob("run-*.jsonl"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


# ---- 结构化模型（读端唯一的「理解」发生处；两个渲染器都只读它）------------------

@dataclass
class NoteItem:
    """会话里的一行旁注（窗口省略提示 / 上下文变短提示等）。"""
    text: str


@dataclass
class MessageItem:
    """request 快照里的一条消息（diff 后保留下来的；含历史 assistant）。"""
    role: str
    content: str = ""
    tool_call_id: Optional[str] = None
    reasoning: Optional[str] = None
    tool_calls: List[dict] = field(default_factory=list)


@dataclass
class ResponseItem:
    """一次 LLM 返回（response 记录）：决策链核心，带耗时/token。"""
    dt: Optional[float] = None
    tokens: Optional[int] = None
    reasoning: Optional[str] = None
    content: str = ""
    tool_calls: List[dict] = field(default_factory=list)


Item = Union[NoteItem, MessageItem, ResponseItem]


@dataclass
class Conv:
    conv_id: str
    n_steps: int                       # 该会话的 LLM 调用次数（request 数）
    items: List[Item] = field(default_factory=list)


@dataclass
class RunNode:
    run_id: str
    agent: str
    provider: str
    model: str
    depth: int
    task: Optional[str]                # 派生：该 run 首条 user 消息（≈ 它被派去做什么）
    stats: dict                        # {"steps", "tokens", "dt"}
    convs: List[Conv] = field(default_factory=list)
    children: List["RunNode"] = field(default_factory=list)


# ---- 建模：records → run 树（应用 diff + 窗口）-------------------------------

def build_tree(records: List[dict], window: int = WINDOW) -> List[RunNode]:
    by_run: Dict[str, List[dict]] = {}
    parent_of: Dict[str, Optional[str]] = {}
    first_seq: Dict[str, int] = {}

    for r in records:
        rid = r.get("run_id", "?")
        by_run.setdefault(rid, []).append(r)
        parent_of.setdefault(rid, r.get("parent_run_id"))
        first_seq.setdefault(rid, r.get("seq", 0))

    children: Dict[Optional[str], List[str]] = {}
    for rid, parent in parent_of.items():
        # 父不在本文件时按根处理（截断的 trace 仍能渲染）
        key = parent if parent in by_run else None
        children.setdefault(key, []).append(rid)
    for kids in children.values():
        kids.sort(key=lambda rid: first_seq.get(rid, 0))

    return [_build_run(rid, 0, by_run, children, window) for rid in children.get(None, [])]


def _build_run(rid, depth, by_run, children, window) -> RunNode:
    recs = sorted(by_run.get(rid, []), key=lambda r: r.get("seq", 0))
    meta = recs[0] if recs else {}

    steps = sum(1 for r in recs if r.get("kind") == "request")
    tokens = sum((r.get("usage") or {}).get("total_tokens", 0) or 0
                 for r in recs if r.get("kind") == "response")
    dt = sum(r.get("dt", 0) or 0 for r in recs if r.get("kind") == "response")

    convs_by_id: Dict[str, List[dict]] = {}
    for r in recs:
        convs_by_id.setdefault(r.get("conv_id", "?"), []).append(r)
    convs = [_build_conv(cid, convs_by_id[cid], window)
             for cid in sorted(convs_by_id, key=lambda c: convs_by_id[c][0].get("seq", 0))]

    return RunNode(
        run_id=rid,
        agent=meta.get("agent", "?"),
        provider=meta.get("provider", "?"),
        model=meta.get("model", "?"),
        depth=depth,
        task=_first_user_task(recs),
        stats={"steps": steps, "tokens": tokens, "dt": dt},
        convs=convs,
        children=[_build_run(c, depth + 1, by_run, children, window)
                  for c in children.get(rid, [])],
    )


def _first_user_task(recs: List[dict]) -> Optional[str]:
    """该 run 首条 request 快照里的第一条 user 消息——对 spoke 即「被派去做什么」。"""
    for r in recs:
        if r.get("kind") == "request":
            for m in r.get("payload", {}).get("snapshot", []):
                if m.get("role") == "user":
                    return m.get("content") or ""
            return None
    return None


def _build_conv(cid, recs, window) -> Conv:
    recs = sorted(recs, key=lambda r: r.get("seq", 0))
    requests = [r for r in recs if r.get("kind") == "request"]

    omitted = max(0, len(requests) - window) if window else 0
    start_seq = requests[omitted].get("seq", 0) if requests else 0

    items: List[Item] = []
    if omitted:
        items.append(NoteItem(f"…（前 {omitted} 步已省略；下方首个快照即含至此的完整历史）"))

    prev_snapshot: Optional[List[dict]] = None
    for r in recs:
        if r.get("seq", 0) < start_seq:
            continue
        if r.get("kind") == "request":
            snap = r.get("payload", {}).get("snapshot", [])
            if prev_snapshot is None:
                for m in snap:                       # 窗口内首个快照：完整 dump
                    items.append(_msg_item(m))
            else:
                i = _common_prefix_len(prev_snapshot, snap)
                for m in snap[i:]:
                    if m.get("role") != "assistant":  # assistant 增量由 response 承载
                        items.append(_msg_item(m))
                if len(snap) < len(prev_snapshot):
                    items.append(NoteItem(
                        f"… 上下文较上一轮变短（{len(prev_snapshot)} → {len(snap)} 条，"
                        "可能被裁剪/压缩）"))
            prev_snapshot = snap
        elif r.get("kind") == "response":
            items.append(_resp_item(r))

    return Conv(conv_id=cid, n_steps=len(requests), items=items)


def _msg_item(m: dict) -> MessageItem:
    return MessageItem(
        role=m.get("role", "?"),
        content=m.get("content") or "",
        tool_call_id=m.get("tool_call_id"),
        reasoning=m.get("reasoning_content"),
        tool_calls=m.get("tool_calls") or [],
    )


def _resp_item(r: dict) -> ResponseItem:
    p = r.get("payload", {})
    usage = r.get("usage") or {}
    return ResponseItem(
        dt=r.get("dt"),
        tokens=usage.get("total_tokens"),
        reasoning=p.get("reasoning_content"),
        content=p.get("content") or "",
        tool_calls=p.get("tool_calls") or [],
    )


def _common_prefix_len(a: List[dict], b: List[dict]) -> int:
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    return i


def _oneline(text: str, max_len: int) -> str:
    t = " ".join((text or "").split())
    return t[:max_len] + "…" if len(t) > max_len else t


# ---- 渲染器 ①：终端 transcript（保持既有输出，测试据此断言）------------------

def render_text(nodes: List[RunNode], max_chars: int = 0) -> str:
    out: List[str] = []
    for n in nodes:
        _text_run(n, out, max_chars)
    return "\n".join(out)


def render_tree(records: List[dict], max_chars: int = 0, window: int = WINDOW) -> str:
    """薄封装：records → 模型 → 终端文本（向后兼容既有调用 / 测试）。"""
    return render_text(build_tree(records, window=window), max_chars=max_chars)


def _text_run(n: RunNode, out, max_chars) -> None:
    pad = "  " * n.depth
    s = n.stats
    out.append(f"{pad}▼ run {n.run_id}  agent={n.agent}  {n.provider}/{n.model}"
               f"  ({s['steps']}步·{s['tokens']}tok·{round(s['dt'], 1)}s)")
    if n.task:
        out.append(f"{pad}  ⟵ 任务：{_oneline(n.task, 60)}")
    for c in n.convs:
        _text_conv(c, n.depth + 1, out, max_chars)
    for child in n.children:
        _text_run(child, out, max_chars)


def _text_conv(c: Conv, depth, out, max_chars) -> None:
    pad = "  " * depth
    out.append(f"{pad}▸ 会话 {c.conv_id}（{c.n_steps} 步 LLM 调用）")
    for it in c.items:
        _text_item(it, depth + 1, out, max_chars)


def _text_item(it: Item, depth, out, max_chars) -> None:
    pad = "  " * depth
    if isinstance(it, NoteItem):
        out.append(f"{pad}{it.text}")
        return
    if isinstance(it, ResponseItem):
        extra = []
        if it.dt is not None:
            extra.append(f"{it.dt}s")
        if it.tokens:
            extra.append(f"{it.tokens} tok")
        tag = f"  ({' · '.join(extra)})" if extra else ""
        out.append(f"{pad}[assistant]{tag}")
        _text_body(it.reasoning, it.content, it.tool_calls, depth, out, max_chars)
        return
    # MessageItem
    if it.role == "tool":
        out.append(f"{pad}[tool] call_id={it.tool_call_id}")
        _block(it.content, depth + 1, out, max_chars)
        return
    if it.role == "assistant":
        out.append(f"{pad}[assistant]")
        _text_body(it.reasoning, it.content, it.tool_calls, depth, out, max_chars)
        return
    out.append(f"{pad}[{it.role}]")
    _block(it.content, depth + 1, out, max_chars)


def _text_body(reasoning, content, tool_calls, depth, out, max_chars) -> None:
    pad = "  " * depth
    if reasoning:
        out.append(f"{pad}  · think:")
        _block(reasoning, depth + 2, out, max_chars)
    if content:
        _block(content, depth + 1, out, max_chars)
    for tc in tool_calls:
        args = json.dumps(tc.get("arguments", {}), ensure_ascii=False)
        if max_chars and len(args) > max_chars:
            args = args[:max_chars] + f"…（截断，原长 {len(args)}）"
        out.append(f"{pad}  → {tc.get('name')}({args})")


def _block(text, depth, out, max_chars) -> None:
    text = text if isinstance(text, str) else str(text)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + f"\n…（截断，原长 {len(text)} 字符）"
    pad = "  " * depth
    for line in text.splitlines() or [""]:
        out.append(f"{pad}{line}")


# ---- 渲染器 ②：HTML 阅读页（单列可折叠嵌套；单文件自包含、零外部依赖）--------
# 长正文 / system prompt / 工具结果 / think / 整棵子 agent 默认折叠，点开按需展开；
# <details>/<summary> 原生折叠，零 JS 即可用，仅「全部展开/折叠」加一点点 JS。

_LONG = 600   # 正文超过这么多字符就默认折叠

_HTML_HEAD = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>replay · {title}</title>
<style>
  :root {{ --line:#e3e3e3; --muted:#888; --user:#1a73e8; --asst:#137333;
           --tool:#b06000; --sys:#6a1b9a; --note:#9aa0a6; --call:#0b7285; }}
  body {{ font: 13px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
          color:#202124; background:#fafafa; margin:0; padding:0 0 40vh; }}
  .toolbar {{ position:sticky; top:0; background:#fff; border-bottom:1px solid var(--line);
              padding:8px 16px; z-index:9; }}
  .toolbar button {{ font:inherit; cursor:pointer; border:1px solid var(--line);
                     background:#fff; border-radius:6px; padding:3px 10px; margin-right:6px; }}
  .toolbar .title {{ color:var(--muted); margin-left:8px; }}
  .tree {{ padding:12px 16px; }}
  details {{ margin:2px 0; }}
  details.run {{ border-left:2px solid var(--line); padding-left:10px; margin:6px 0; }}
  summary {{ cursor:pointer; outline:none; }}
  summary::-webkit-details-marker {{ color:var(--muted); }}
  .run > summary {{ font-weight:600; }}
  .agent {{ color:var(--asst); }}
  .rid {{ color:var(--muted); font-weight:400; margin-left:8px; }}
  .badge {{ display:inline-block; background:#eef0f3; color:#555; border-radius:10px;
            padding:0 7px; margin-left:6px; font-size:11px; font-weight:400; }}
  .task {{ color:var(--muted); margin:2px 0 4px 14px; }}
  .task::before {{ content:"⟵ "; }}
  .msg {{ margin:3px 0 3px 14px; }}
  .role {{ color:var(--muted); }}
  .msg.resp > .role {{ color:var(--asst); }}
  .msg.user > .role {{ color:var(--user); }}
  .system > summary {{ color:var(--sys); }}
  .tool > summary {{ color:var(--tool); }}
  .think > summary {{ color:var(--muted); }}
  .system, .tool, .think, .long, .call {{ margin-left:14px; }}
  .note {{ color:var(--note); margin:3px 0 3px 14px; font-style:italic; }}
  .call {{ color:var(--call); }}
  .call > summary {{ color:var(--call); }}
  pre {{ margin:2px 0 2px 14px; white-space:pre-wrap; word-break:break-word;
         background:#fff; border:1px solid var(--line); border-radius:6px; padding:6px 9px; }}
  .conv > summary {{ color:var(--muted); }}
</style></head><body>
<div class="toolbar">
  <button onclick="setAll(true)">全部展开</button>
  <button onclick="setAll(false)">全部折叠</button>
  <span class="title">{title}</span>
</div>
<div class="tree">
"""

_HTML_TAIL = """</div>
<script>
  function setAll(open) {
    document.querySelectorAll('details').forEach(d => d.open = open);
  }
</script>
</body></html>
"""


def render_html(nodes: List[RunNode], title: str = "replay") -> str:
    parts = [_HTML_HEAD.format(title=html.escape(title))]
    for n in nodes:
        _html_run(n, parts)
    parts.append(_HTML_TAIL)
    return "".join(parts)


def _html_run(n: RunNode, parts) -> None:
    s = n.stats
    open_attr = " open" if n.depth == 0 else ""   # 根展开，子 agent 默认折叠
    parts.append(f'<details class="run"{open_attr}><summary>'
                 f'<span class="agent">{html.escape(n.agent)}</span>'
                 f'<span class="rid">run {html.escape(n.run_id)}</span>'
                 f'<span class="badge">{s["steps"]} 步</span>'
                 f'<span class="badge">{s["tokens"]} tok</span>'
                 f'<span class="badge">{round(s["dt"], 1)}s</span>'
                 '</summary>')
    if n.task:
        parts.append(f'<div class="task">{html.escape(_oneline(n.task, 120))}</div>')

    if len(n.convs) == 1:                          # 单会话：直接铺 item，省一层嵌套
        for it in n.convs[0].items:
            _html_item(it, parts)
    else:
        for c in n.convs:
            parts.append(f'<details class="conv" open><summary>'
                         f'会话 {html.escape(c.conv_id)} · {c.n_steps} 步</summary>')
            for it in c.items:
                _html_item(it, parts)
            parts.append('</details>')

    for child in n.children:
        _html_run(child, parts)
    parts.append('</details>')


def _html_item(it: Item, parts) -> None:
    if isinstance(it, NoteItem):
        parts.append(f'<div class="note">{html.escape(it.text)}</div>')
        return
    if isinstance(it, ResponseItem):
        parts.append(f'<div class="msg resp"><div class="role">[assistant]'
                     f'{_html_badges(it.dt, it.tokens)}</div>')
        _html_body(it.reasoning, it.content, it.tool_calls, parts)
        parts.append('</div>')
        return
    # MessageItem
    if it.role == "system":
        parts.append('<details class="system"><summary>[system prompt]</summary>'
                     f'<pre>{html.escape(it.content)}</pre></details>')
        return
    if it.role == "tool":
        parts.append(f'<details class="tool"><summary>[tool] call_id='
                     f'{html.escape(str(it.tool_call_id))}</summary>'
                     f'<pre>{html.escape(it.content)}</pre></details>')
        return
    if it.role == "assistant":
        parts.append('<div class="msg asst"><div class="role">[assistant]</div>')
        _html_body(it.reasoning, it.content, it.tool_calls, parts)
        parts.append('</div>')
        return
    parts.append(f'<div class="msg user"><div class="role">[{html.escape(it.role)}]</div>')
    _html_content(it.content, parts)
    parts.append('</div>')


def _html_body(reasoning, content, tool_calls, parts) -> None:
    if reasoning:
        parts.append('<details class="think"><summary>· think</summary>'
                     f'<pre>{html.escape(reasoning)}</pre></details>')
    if content:
        _html_content(content, parts)
    for tc in tool_calls:
        args = json.dumps(tc.get("arguments", {}), ensure_ascii=False)
        name = html.escape(str(tc.get("name")))
        if len(args) > 200:
            parts.append(f'<details class="call"><summary>→ {name}(…{len(args)} 字)</summary>'
                         f'<pre>{html.escape(args)}</pre></details>')
        else:
            parts.append(f'<div class="call">→ {name}({html.escape(args)})</div>')


def _html_content(content: str, parts) -> None:
    content = content or ""
    if len(content) > _LONG:
        parts.append(f'<details class="long"><summary>{len(content)} 字，点开</summary>'
                     f'<pre>{html.escape(content)}</pre></details>')
    else:
        parts.append(f'<pre>{html.escape(content)}</pre>')


def _html_badges(dt, tokens) -> str:
    out = ""
    if dt is not None:
        out += f'<span class="badge">{dt}s</span>'
    if tokens:
        out += f'<span class="badge">{tokens} tok</span>'
    return out


# ---- CLI --------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把 trace JSONL 按层级还原成 transcript")
    ap.add_argument("path", nargs="?", help="trace 文件；省略则取 TRACE_DIR 里最新的")
    ap.add_argument("--max", type=int, default=0, help="长正文截断字符数（默认 0=不截断，仅文本）")
    ap.add_argument("--window", type=int, default=WINDOW,
                    help=f"每条会话渲染最近几个 request 快照（默认 {WINDOW}，0=不限）")
    ap.add_argument("--html", nargs="?", const="", metavar="OUT",
                    help="生成 HTML 阅读页；省略路径则写到 <trace>.html 旁边")
    ap.add_argument("--open", action="store_true",
                    help="生成 HTML 后用默认浏览器打开（仅配合 --html）")
    args = ap.parse_args(argv)

    if args.path:
        path = Path(args.path)
    else:
        trace_dir = Path(os.getenv("TRACE_DIR", "traces"))
        path = latest_trace(trace_dir)
        if path is None:
            print(f"未找到 trace 文件（在 {trace_dir}/）。先跑一次 agent，或指定路径。",
                  file=sys.stderr)
            return 1
    if not path.is_file():
        print(f"文件不存在：{path}", file=sys.stderr)
        return 1

    records = load_records(path)
    nodes = build_tree(records, window=args.window)

    if args.html is not None:
        out_path = Path(args.html) if args.html else path.with_suffix(".html")
        out_path.write_text(render_html(nodes, title=path.name), encoding="utf-8")
        print(f"# replay HTML 已写入 {out_path}")
        if args.open:
            webbrowser.open(out_path.resolve().as_uri())
        return 0

    if args.open:
        print("--open 仅在配合 --html 时有效（文本模式无可打开的文件）。", file=sys.stderr)
    print(f"# replay {path}\n")
    print(render_text(nodes, max_chars=args.max))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
