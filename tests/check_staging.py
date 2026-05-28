"""
Step 3 验证：core/staging.py 的两条受控搬运通道。

  外部 path ──import_files──► uploads/  ─┐
                                          ├──stage_files──► wiki/staging/
        reports/<filename>  ─────────────┘

覆盖：
  1. case_import_basic：外部 .md → uploads/，返回相对 cwd 路径
  2. case_import_rejects：不存在 / 后缀拒绝 / 超大 / 空列表
  3. case_import_conflict：同名冲突自动加时间戳后缀（不覆盖）
  4. case_stage_basic：reports/ 与 uploads/ 下的文件 → wiki/staging/
  5. case_stage_rejects：非 reports/uploads 前缀 / 不存在 / 越界
  6. case_end_to_end：import_files → stage_files 双步联动，路径互相承接
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.staging import MAX_BYTES, ImportFilesTool, StageFilesTool   # noqa: E402


async def case_import_basic():
    print("\n[1] import_files：外部 .md → uploads/")
    with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
        ext = Path(tmp) / "external" / "note.md"
        ext.parent.mkdir(parents=True)
        ext.write_text("外部笔记内容", encoding="utf-8")

        tool = ImportFilesTool()
        obs = await tool.execute(paths=[str(ext)])
        assert "✓" in obs and "uploads/note.md" in obs, f"obs:\n{obs}"
        assert Path("uploads/note.md").is_file(), "目标文件落盘"
        assert Path("uploads/note.md").read_text(encoding="utf-8") == "外部笔记内容"
        print("  case_import_basic OK")


async def case_import_rejects():
    print("\n[2] import_files：边界拒绝（不存在 / 后缀 / 超大 / 空）")
    with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
        binfile = Path(tmp) / "secret.bin"
        binfile.write_bytes(b"\x00\x01")
        toobig = Path(tmp) / "big.md"
        toobig.write_bytes(b"x" * (MAX_BYTES + 1))

        tool = ImportFilesTool()
        obs = await tool.execute(paths=[
            "/nonexistent/path.md",
            str(binfile),
            str(toobig),
        ])
        assert "文件不存在" in obs, f"obs:\n{obs}"
        assert "后缀不在白名单" in obs, f"obs:\n{obs}"
        assert "超过上限" in obs, f"obs:\n{obs}"
        # 全是 ✗，不应有 ✓
        assert "✓" not in obs, f"obs 不该有 ✓:\n{obs}"

        empty_obs = await tool.execute(paths=[])
        assert empty_obs.startswith("Error: paths 不能为空"), empty_obs
        print("  case_import_rejects OK")


async def case_import_conflict():
    print("\n[3] import_files：同名冲突自动加时间戳后缀")
    with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
        ext = Path(tmp) / "note.md"
        ext.write_text("原始", encoding="utf-8")

        tool = ImportFilesTool()
        first = await tool.execute(paths=[str(ext)])
        ext.write_text("修改后", encoding="utf-8")
        second = await tool.execute(paths=[str(ext)])

        upload_files = sorted(p.name for p in Path("uploads").glob("*.md"))
        assert len(upload_files) == 2, f"应有 2 个文件（含时间戳变体），实际：{upload_files}"
        assert "note.md" in upload_files, upload_files
        # 第二份内容应是「修改后」，且文件名带时间戳后缀
        ts_file = next(f for f in upload_files if f != "note.md")
        assert ts_file.startswith("note-") and ts_file.endswith(".md"), ts_file
        assert Path(f"uploads/{ts_file}").read_text(encoding="utf-8") == "修改后"
        print(f"  case_import_conflict OK（生成 {upload_files}）")


async def case_stage_basic():
    print("\n[4] stage_files：reports/ + uploads/ → wiki/staging/")
    with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
        Path("reports").mkdir()
        Path("uploads").mkdir()
        Path("reports/rl-survey.md").write_text("RL 报告", encoding="utf-8")
        Path("uploads/user-note.md").write_text("用户笔记", encoding="utf-8")

        tool = StageFilesTool()
        obs = await tool.execute(paths=[
            "reports/rl-survey.md",
            "uploads/user-note.md",
        ])
        assert "staging/rl-survey.md" in obs, f"obs:\n{obs}"
        assert "staging/user-note.md" in obs, f"obs:\n{obs}"
        assert Path("wiki/staging/rl-survey.md").is_file()
        assert Path("wiki/staging/user-note.md").is_file()
        assert Path("wiki/staging/rl-survey.md").read_text(encoding="utf-8") == "RL 报告"
        print("  case_stage_basic OK")


async def case_stage_rejects():
    print("\n[5] stage_files：非工作区前缀 / 不存在 / 越界")
    with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
        Path("reports").mkdir()
        Path("uploads").mkdir()
        Path("evil.md").write_text("外部邻居", encoding="utf-8")

        tool = StageFilesTool()
        obs = await tool.execute(paths=[
            "evil.md",                     # 非 reports/uploads 前缀
            "reports/nope.md",             # 不存在
            "reports/../evil.md",          # 形式合规但解析后越界
        ])
        assert "只允许 reports/ 或 uploads/ 路径" in obs, f"obs:\n{obs}"
        assert "文件不存在" in obs, f"obs:\n{obs}"
        assert "解析后越界" in obs, f"obs:\n{obs}"
        assert "✓" not in obs, f"obs 不该有 ✓:\n{obs}"
        # 不应有任何文件被复制
        staging = Path("wiki/staging")
        copied = list(staging.glob("*")) if staging.exists() else []
        assert not copied, f"被拒后不应有文件落入 staging：{copied}"
        print("  case_stage_rejects OK")


async def case_end_to_end():
    print("\n[6] 端到端：import_files → stage_files，路径互相承接")
    with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
        ext = Path(tmp) / "ext.md"
        ext.write_text("用户提供的文档", encoding="utf-8")

        # 模拟 ResearchAgent 落盘
        Path("reports").mkdir()
        Path("reports/research-output.md").write_text("研究产出", encoding="utf-8")

        importer = ImportFilesTool()
        stager = StageFilesTool()

        # Step 1: 把外部文件导入 uploads/
        imp_obs = await importer.execute(paths=[str(ext)])
        assert "✓" in imp_obs and "uploads/ext.md" in imp_obs, f"import obs:\n{imp_obs}"

        # Step 2: 把 reports + uploads 的产物 stage 到 wiki/staging/
        stg_obs = await stager.execute(paths=[
            "reports/research-output.md",
            "uploads/ext.md",
        ])
        assert "staging/research-output.md" in stg_obs, f"stage obs:\n{stg_obs}"
        assert "staging/ext.md" in stg_obs, f"stage obs:\n{stg_obs}"
        assert Path("wiki/staging/research-output.md").is_file()
        assert Path("wiki/staging/ext.md").is_file()
        # staging 内容与源一致
        assert Path("wiki/staging/ext.md").read_text(encoding="utf-8") == "用户提供的文档"
        print("  case_end_to_end OK")


async def main() -> None:
    await case_import_basic()
    await case_import_rejects()
    await case_import_conflict()
    await case_stage_basic()
    await case_stage_rejects()
    await case_end_to_end()
    print("\nOK")


if __name__ == "__main__":
    asyncio.run(main())
