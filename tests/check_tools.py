"""
Step C 验证：core/tools.py 工具层 + ./wiki/ 沙箱（离线，无 LLM / 无 API）。

覆盖：冷启动种 index.md、specs()、读写往返、递归列举相对路径、.md 限制、
沙箱挡穿越/绝对路径/符号链接、未知工具、坏参数——全部收敛为 observation 字符串。

跑法：
  PY=/usr/local/Caskroom/miniforge/base/envs/claude-deepseek/bin/python
  $PY tests/check_tools.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm.base import ToolCall
from core.tools import SandboxViolation, build_wiki_registry


def call(name: str, **args) -> ToolCall:
    return ToolCall(id="t", name=name, arguments=args)


async def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str):
        if cond:
            print(f"  ✓ {msg}")
        else:
            failures.append(msg)
            print(f"  ✗ {msg}")

    with tempfile.TemporaryDirectory() as tmp:
        wiki = Path(tmp) / "wiki"
        reg = build_wiki_registry(wiki)

        print("[冷启动]")
        check((wiki / "index.md").is_file(), "首次构造种入了 index.md 骨架")
        names = sorted(s.name for s in reg.specs())
        check(names == ["list_files", "read_file", "read_source", "write_file"],
              f"specs() 暴露 4 工具: {names}")

        print("\n[读写往返 + 自动建父目录]")
        r = await reg.execute(call("write_file", path="AI/transformer.md", content="# Transformer\n注意力机制"))
        check(r.startswith("已写入"), f"write_file 成功: {r!r}")
        check((wiki / "AI" / "transformer.md").is_file(), "父目录 AI/ 被自动创建且文件落盘")
        r = await reg.execute(call("read_file", path="AI/transformer.md"))
        check("注意力机制" in r, "read_file 读回内容")

        print("\n[list_files 递归 + 相对路径]")
        r = await reg.execute(call("list_files"))
        lines = set(r.splitlines())
        check(lines == {"index.md", "AI/transformer.md"}, f"递归列出相对路径: {lines}")
        check("/" not in r.split("AI/")[0], "未泄露绝对路径（无沙箱根前缀）")

        print("\n[.md 限制]")
        r = await reg.execute(call("write_file", path="notes.txt", content="x"))
        check(r.startswith("Error:") and ".md" in r, f"拒绝非 .md 写入: {r!r}")
        check(not (wiki / "notes.txt").exists(), "非 .md 文件确实未被写入")

        print("\n[沙箱：穿越 / 绝对路径]")
        r = await reg.execute(call("read_file", path="../../etc/passwd"))
        check(r.startswith("Error:") and "沙箱外" in r, f"挡住路径穿越: {r!r}")
        r = await reg.execute(call("write_file", path="/tmp/evil.md", content="x"))
        check(r.startswith("Error:") and "沙箱外" in r, f"挡住绝对路径: {r!r}")
        check(not Path("/tmp/evil.md").exists(), "越界写入确实未执行")

        print("\n[沙箱：符号链接逃逸]")
        secret = Path(tmp) / "secret.md"
        secret.write_text("TOP SECRET", encoding="utf-8")
        link = wiki / "link.md"
        os.symlink(secret, link)
        r = await reg.execute(call("read_file", path="link.md"))
        check(r.startswith("Error:") and "沙箱外" in r, f"挡住符号链接逃逸: {r!r}")

        print("\n[未知工具 / 坏参数 收敛为字符串]")
        r = await reg.execute(call("nope"))
        check(r.startswith("Error:") and "未知工具" in r, f"未知工具: {r!r}")
        r = await reg.execute(call("read_file"))  # 缺 path
        check(r.startswith("Error:") and "参数不匹配" in r, f"缺参数: {r!r}")
        r = await reg.execute(call("read_file", path="x.md", bogus=1))  # 多余参数
        check(r.startswith("Error:"), f"多余参数: {r!r}")
        r = await reg.execute(call("read_file", path="missing.md"))
        check(r.startswith("Error:") and "不存在" in r, f"文件不存在: {r!r}")

        print("\n[_resolve 单元：越界直接抛 SandboxViolation]")
        read_tool = reg._tools["read_file"]
        try:
            read_tool._resolve("../escape")
            check(False, "越界应抛 SandboxViolation")
        except SandboxViolation:
            check(True, "_resolve 越界抛 SandboxViolation（registry 才转字符串）")

    print()
    if failures:
        print(f"❌ {len(failures)} 项失败")
        return 1
    print("✅ Step C 工具层 + 沙箱全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
