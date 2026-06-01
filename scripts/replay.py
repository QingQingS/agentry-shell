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

用法：
    python scripts/replay.py [trace.jsonl]   # 省略则取 TRACE_DIR(默认 traces/) 里最新的
    python scripts/replay.py --max 800        # 长正文截断到 800 字符（默认 0=不截断）
    python scripts/replay.py --window 0        # 关掉读时窗口（每条会话全量展开）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

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


# ---- 渲染（纯函数，便于单测）------------------------------------------------

def render_tree(records: List[dict], max_chars: int = 0, window: int = WINDOW) -> str:
    """records → 多行字符串。按 run 树深度优先；每个 run 内按 conv_id 分会话。"""
    by_run: Dict[str, List[dict]] = {}
    parent_of: Dict[str, Optional[str]] = {}
    children: Dict[Optional[str], List[str]] = {}
    first_seq: Dict[str, int] = {}

    for r in records:
        rid = r.get("run_id", "?")
        by_run.setdefault(rid, []).append(r)
        if rid not in parent_of:
            parent_of[rid] = r.get("parent_run_id")
        first_seq.setdefault(rid, r.get("seq", 0))

    for rid, parent in parent_of.items():
        # 父不在本文件时按根处理（截断的 trace 仍能渲染）
        key = parent if parent in by_run else None
        children.setdefault(key, []).append(rid)
    for kids in children.values():
        kids.sort(key=lambda rid: first_seq.get(rid, 0))

    out: List[str] = []
    for root in children.get(None, []):
        _render_run(root, 0, by_run, children, out, max_chars, window)
    return "\n".join(out)


def _render_run(rid, depth, by_run, children, out, max_chars, window) -> None:
    pad = "  " * depth
    recs = sorted(by_run.get(rid, []), key=lambda r: r.get("seq", 0))
    meta = recs[0] if recs else {}
    agent = meta.get("agent", "?")
    model = f"{meta.get('provider', '?')}/{meta.get('model', '?')}"
    out.append(f"{pad}▼ run {rid}  agent={agent}  {model}")

    # 一个 run 内按 conv_id 分会话（默认一 run 一会话；保序按各会话首条 seq）
    convs: Dict[str, List[dict]] = {}
    for r in recs:
        convs.setdefault(r.get("conv_id", "?"), []).append(r)
    for conv_id in sorted(convs, key=lambda c: convs[c][0].get("seq", 0)):
        _render_conversation(conv_id, convs[conv_id], depth + 1, out, max_chars, window)

    for child in children.get(rid, []):
        _render_run(child, depth + 1, by_run, children, out, max_chars, window)


def _render_conversation(conv_id, recs, depth, out, max_chars, window) -> None:
    pad = "  " * depth
    recs = sorted(recs, key=lambda r: r.get("seq", 0))
    requests = [r for r in recs if r.get("kind") == "request"]

    # 读时窗口：只渲染最近 window 个 request 快照
    omitted = max(0, len(requests) - window) if window else 0
    start_seq = requests[omitted].get("seq", 0) if requests else 0

    out.append(f"{pad}▸ 会话 {conv_id}（{len(requests)} 步 LLM 调用）")
    if omitted:
        out.append(f"{pad}  …（前 {omitted} 步已省略；下方首个快照即含至此的完整历史）")

    prev_snapshot: Optional[List[dict]] = None
    for r in recs:
        if r.get("seq", 0) < start_seq:
            continue
        if r.get("kind") == "request":
            snap = r.get("payload", {}).get("snapshot", [])
            if prev_snapshot is None:
                for m in snap:                       # 窗口内首个快照：完整 dump
                    _render_message(m, depth + 1, out, max_chars)
            else:
                _render_diff(prev_snapshot, snap, depth + 1, out, max_chars)
            prev_snapshot = snap
        elif r.get("kind") == "response":
            _render_response(r, depth + 1, out, max_chars)


def _common_prefix_len(a: List[dict], b: List[dict]) -> int:
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    return i


def _render_diff(prev: List[dict], cur: List[dict], depth, out, max_chars) -> None:
    """相邻快照 diff：渲染本轮相对上一轮新增的尾巴（assistant 增量由 response 承载，跳过）。

    无须理解 input 内部结构——纯按消息序列比对：
      · 正常延长（cur 接在 prev 后）→ 只多出 assistant + 新工具结果，跳过 assistant；
      · 末尾临时项被顶替（如逐轮变化的健康度仪表盘）→ 公共前缀止于真历史，新仪表盘
        作为「新增非 assistant」如实显示一次，不告警（它本就每轮在变）；
      · 真正变短（裁剪/压缩）→ 标注一行，提示上下文被截断。
    """
    i = _common_prefix_len(prev, cur)
    for m in cur[i:]:
        if m.get("role") != "assistant":      # assistant 由对应 response 记录渲染
            _render_message(m, depth, out, max_chars)
    if len(cur) < len(prev):
        pad = "  " * depth
        out.append(f"{pad}… 上下文较上一轮变短（{len(prev)} → {len(cur)} 条，可能被裁剪/压缩）")


def _render_message(m: dict, depth, out, max_chars) -> None:
    """渲染快照里的一条消息（含历史 assistant——它对应的 response 记录可能已被窗口略去）。"""
    pad = "  " * depth
    role = m.get("role", "?")
    if role == "tool":
        out.append(f"{pad}[tool] call_id={m.get('tool_call_id')}")
        _block(m.get("content", ""), depth + 1, out, max_chars)
        return
    if role == "assistant":
        out.append(f"{pad}[assistant]")
        _render_assistant_body(m, depth, out, max_chars)
        return
    out.append(f"{pad}[{role}]")
    _block(m.get("content", ""), depth + 1, out, max_chars)


def _render_response(r: dict, depth, out, max_chars) -> None:
    pad = "  " * depth
    p = r.get("payload", {})
    extra = []
    if r.get("dt") is not None:
        extra.append(f"{r['dt']}s")
    usage = r.get("usage") or {}
    if usage.get("total_tokens"):
        extra.append(f"{usage['total_tokens']} tok")
    tag = f"  ({' · '.join(extra)})" if extra else ""
    out.append(f"{pad}[assistant]{tag}")
    _render_assistant_body(p, depth, out, max_chars)


def _render_assistant_body(p: dict, depth, out, max_chars) -> None:
    pad = "  " * depth
    if p.get("reasoning_content"):
        out.append(f"{pad}  · think:")
        _block(p["reasoning_content"], depth + 2, out, max_chars)
    if p.get("content"):
        _block(p["content"], depth + 1, out, max_chars)
    for tc in p.get("tool_calls", []):
        args = json.dumps(tc.get("arguments", {}), ensure_ascii=False)
        if max_chars and len(args) > max_chars:
            args = args[:max_chars] + f"…（截断，原长 {len(args)}）"
        out.append(f"{pad}  → {tc.get('name')}({args})")


def _block(text: str, depth, out, max_chars) -> None:
    text = text if isinstance(text, str) else str(text)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + f"\n…（截断，原长 {len(text)} 字符）"
    pad = "  " * depth
    for line in text.splitlines() or [""]:
        out.append(f"{pad}{line}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把 trace JSONL 按层级还原成 transcript")
    ap.add_argument("path", nargs="?", help="trace 文件；省略则取 TRACE_DIR 里最新的")
    ap.add_argument("--max", type=int, default=0, help="长正文截断字符数（默认 0=不截断）")
    ap.add_argument("--window", type=int, default=WINDOW,
                    help=f"每条会话渲染最近几个 request 快照（默认 {WINDOW}，0=不限）")
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

    print(f"# replay {path}\n")
    print(render_tree(load_records(path), max_chars=args.max, window=args.window))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
