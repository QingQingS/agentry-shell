#!/usr/bin/env python3
"""
replay —— 把一次 run 的 trace（core/trace.py 落的 JSONL）按层级还原成人读的 transcript。

这是「复盘」的最终出口：步1 已把每次 LLM 调用的无损增量落盘，步2 给每条记录补了
run_id/parent_run_id/agent。本工具据此——
  - 按 parent_run_id 把记录拼成 hub→spoke→子调用的树；
  - 按 seq 还原每个 run 内的逐轮 transcript；
  - 完整渲染此前永远拿不回的三样：每个 agent 的完整 system prompt、LLM 原样输出的
    tool_call 全参数（含整篇报告）、LLM 实际收到的工具结果原文。

用法：
    python scripts/replay.py [trace.jsonl]   # 省略则取 TRACE_DIR(默认 traces/) 里最新的
    python scripts/replay.py --max 800        # 长正文截断到 800 字符（默认 0=不截断）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional


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

def render_tree(records: List[dict], max_chars: int = 0) -> str:
    """records → 多行字符串。按 run 树深度优先渲染，每个 run 内按 seq。"""
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
        _render_run(root, 0, by_run, children, out, max_chars)
    return "\n".join(out)


def _render_run(rid, depth, by_run, children, out, max_chars) -> None:
    pad = "  " * depth
    recs = sorted(by_run.get(rid, []), key=lambda r: r.get("seq", 0))
    meta = recs[0] if recs else {}
    agent = meta.get("agent", "?")
    model = f"{meta.get('provider', '?')}/{meta.get('model', '?')}"
    out.append(f"{pad}▼ run {rid}  agent={agent}  {model}")
    for r in recs:
        _render_record(r, depth + 1, out, max_chars)
    for child in children.get(rid, []):
        _render_run(child, depth + 1, by_run, children, out, max_chars)


def _render_record(r: dict, depth, out, max_chars) -> None:
    pad = "  " * depth
    p = r.get("payload", {})
    if r.get("kind") == "msg":
        role = p.get("role", "?")
        if role == "tool":
            out.append(f"{pad}[tool] call_id={p.get('tool_call_id')}")
        else:
            out.append(f"{pad}[{role}]")
        _block(p.get("content", ""), depth + 1, out, max_chars)
        return

    # response（assistant 增量）
    extra = []
    if r.get("dt") is not None:
        extra.append(f"{r['dt']}s")
    usage = r.get("usage") or {}
    if usage.get("total_tokens"):
        extra.append(f"{usage['total_tokens']} tok")
    tag = f"  ({' · '.join(extra)})" if extra else ""
    out.append(f"{pad}[assistant]{tag}")
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
    print(render_tree(load_records(path), max_chars=args.max))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
