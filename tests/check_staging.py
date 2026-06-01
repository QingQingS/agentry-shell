"""check_staging.py —— ImportFilesTool + staging pre-hook 的离线验证。

跑：python tests/check_staging.py
断言：
  ImportFilesTool —— 合法入库、外部 path 收口 uploads/、后缀白名单、冲突加时间戳。
  stage_one —— 拍平命名、幂等跳过（同内容重试无副作用）、同名异内容报错、
               前缀/越界/不存在拒绝。
  stage_wiki_inputs —— 就地改写 payload['files'] 成 staging 文件名、失败短路返回错误串、
               重复调用幂等（不再像旧 StageFilesTool 那样换时间戳堆积）。
全程文件系统操作，无 LLM、无网络。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.staging import (  # noqa: E402
    ImportFilesTool,
    StageError,
    stage_one,
    stage_wiki_inputs,
)


def _write(p: Path, text: str = "hello") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


async def _check_import() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        uploads = tmp / "uploads"
        ext = tmp / "external"
        _write(ext / "note.md", "external note")
        tool = ImportFilesTool(uploads_root=uploads)

        out = await tool.execute(paths=[str(ext / "note.md")])
        assert "✓" in out and "note.md" in out, out
        assert (uploads / "note.md").exists()

        _write(ext / "bad.exe", "x")
        out = await tool.execute(paths=[str(ext / "bad.exe")])
        assert "后缀不在白名单" in out, out

        # 重复入库 → 加时间戳，不覆盖（uploads 是归档性存储，防覆盖是对的）
        out = await tool.execute(paths=[str(ext / "note.md")])
        assert "✓" in out, out
        files = sorted(p.name for p in uploads.glob("note*"))
        assert len(files) == 2, files


def _check_stage_one() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        reports = (tmp / "reports").resolve()
        uploads = (tmp / "uploads").resolve()
        staging = (tmp / "wiki" / "staging").resolve()
        os.chdir(tmp)  # stage_one 接收 cwd 相对的 src（reports/...）
        _write(reports / "r1.md", "body one")

        # 拍平命名：reports/r1.md → reports__r1.md
        dest = stage_one(
            "reports/r1.md",
            reports_root=reports,
            uploads_root=uploads,
            staging_root=staging,
        )
        assert dest.name == "reports__r1.md", dest
        assert dest.read_text(encoding="utf-8") == "body one"

        # 幂等：同内容重试 → 同一目标，不新增文件
        dest2 = stage_one(
            "reports/r1.md",
            reports_root=reports,
            uploads_root=uploads,
            staging_root=staging,
        )
        assert dest2 == dest
        assert len(list(staging.glob("*.md"))) == 1, list(staging.glob("*.md"))

        # 同名异内容 → 报错（机械故障，不覆盖 / 不加时间戳）
        _write(reports / "r1.md", "DIFFERENT body")
        try:
            stage_one(
                "reports/r1.md",
                reports_root=reports,
                uploads_root=uploads,
                staging_root=staging,
            )
            assert False, "应抛 StageError"
        except StageError as e:
            assert "内容不同" in str(e), e

        # 非法前缀拒绝
        try:
            stage_one(
                "etc/passwd.md",
                reports_root=reports,
                uploads_root=uploads,
                staging_root=staging,
            )
            assert False, "应抛 StageError"
        except StageError as e:
            assert "前缀" in str(e), e

        # 源不存在拒绝
        try:
            stage_one(
                "reports/missing.md",
                reports_root=reports,
                uploads_root=uploads,
                staging_root=staging,
            )
            assert False, "应抛 StageError"
        except StageError as e:
            assert "不存在" in str(e), e


def _check_pre_hook() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        os.chdir(tmp)  # stage_wiki_inputs 走 cwd 相对默认根
        _write(Path("reports/rl.md"), "rl report")
        _write(Path("uploads/ext.md"), "ext doc")

        # 改写 payload['files'] 成 staging 文件名；成功返回 None
        payload = {"prompt": "归档", "context": "", "files": ["reports/rl.md", "uploads/ext.md"]}
        err = stage_wiki_inputs(payload)
        assert err is None, err
        assert payload["files"] == ["reports__rl.md", "uploads__ext.md"], payload["files"]
        staging = Path("wiki/staging")
        assert (staging / "reports__rl.md").exists()
        assert (staging / "uploads__ext.md").exists()

        # 重复派发（失败重试场景）→ 幂等：staging 文件数不增、不出现时间戳变体
        payload2 = {"prompt": "归档", "context": "", "files": ["reports/rl.md"]}
        assert stage_wiki_inputs(payload2) is None
        assert payload2["files"] == ["reports__rl.md"]
        assert len(list(staging.glob("*.md"))) == 2, list(staging.glob("*.md"))

        # 非法源 → 短路返回错误字符串，payload 不被部分改写成功态
        bad = {"prompt": "归档", "context": "", "files": ["etc/passwd.md"]}
        err = stage_wiki_inputs(bad)
        assert err is not None and "staging 失败" in err, err

        # 无 files → 无操作、返回 None（原文可能已在 prompt 里）
        empty = {"prompt": "归档", "context": "", "files": []}
        assert stage_wiki_inputs(empty) is None


async def _run() -> None:
    cwd = os.getcwd()
    try:
        await _check_import()
        _check_stage_one()
        _check_pre_hook()
    finally:
        os.chdir(cwd)
    print("check_staging OK")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
