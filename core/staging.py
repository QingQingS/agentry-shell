"""
Coordinator 端的工作区搬运工具（v2 hub-and-spoke 数据流的「闸口」）。

跨 agent 数据通过文件系统传递，本模块定义两条受控通道：

  外部 path ──import_files──► uploads/  ─┐
                                          ├──stage_files──► wiki/staging/ ──read_source──► WikiAgent
        ResearchAgent.save_report ──► reports/ ─┘

- import_files：唯一允许接受「外部任意 path」的工具——Coordinator 用它把用户提到的
  外部文件收口到 uploads/，避免下游 spoke 直接读外部路径破坏沙箱。
- stage_files：只接受 reports/ 或 uploads/ 下的工作区路径，把它们复制到
  wiki/staging/；这是 WikiAgent.read_source 能看到的唯一目录。

设计约束：
- 都继承 core/tools.py 的 Tool ABC，可直接放进 Coordinator 的 ToolRegistry。
- 工具内部错误（路径不存在 / 后缀拒绝 / 越界 / 超限）都返回 observation 字符串，
  绝不抛——遵循 wiki 工具层的传统，循环不会被工具异常打断。
- 默认根目录走 cwd 相对路径（./uploads, ./reports, ./wiki/staging），测试时通过
  构造参数注入或 chdir 隔离。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import List, Optional

from core.llm.base import ToolSpec
from core.tools import Tool

MAX_BYTES = 1024 * 1024  # 1 MiB —— 与 ReadSourceTool 对齐
ALLOWED_SUFFIXES = {".md", ".markdown", ".txt", ".rst"}


def _ts_suffix() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _display(p: Path) -> str:
    """优先用相对 cwd 的路径展示，便于 LLM 在后续工具调用中直接引用。"""
    try:
        return str(p.relative_to(Path.cwd()))
    except ValueError:
        return str(p)


def _resolve_with_conflict(target: Path) -> Path:
    """若目标存在则追加时间戳后缀，避免覆盖。"""
    if not target.exists():
        return target
    return target.with_name(f"{target.stem}-{_ts_suffix()}{target.suffix}")


class ImportFilesTool(Tool):
    """把用户提到的外部文件复制到 uploads/，作为后续 stage_files 的源头。

    跨 agent 工作流的「外部入口」唯一收敛点：用户提到的任何外部文件都通过此工具
    进入工作区（uploads/），下游 spoke 永远不直接读外部 path。
    """

    DEFAULT_UPLOADS = "uploads"

    spec = ToolSpec(
        name="import_files",
        description=(
            "把用户提到的外部文件复制到 uploads/。paths 是外部路径列表（相对或绝对均可，"
            "支持 ~ 展开）。返回每个文件入库后的工作区路径或失败原因。"
            "只接受 .md/.markdown/.txt/.rst；单文件 ≤ 1 MiB；同名冲突自动加时间戳。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "外部文件路径列表",
                }
            },
            "required": ["paths"],
        },
    )

    def __init__(self, uploads_root: Optional[Path] = None):
        root = Path(uploads_root) if uploads_root else Path(self.DEFAULT_UPLOADS)
        self.root = root.resolve()

    async def execute(self, paths: List[str]) -> str:
        if not paths:
            return "Error: paths 不能为空"
        self.root.mkdir(parents=True, exist_ok=True)
        lines: List[str] = []
        for raw in paths:
            try:
                src = Path(raw).expanduser().resolve()
            except (OSError, RuntimeError) as e:
                lines.append(f"✗ {raw}: 路径解析失败 ({e})")
                continue
            if not src.is_file():
                lines.append(f"✗ {raw}: 文件不存在")
                continue
            if src.suffix.lower() not in ALLOWED_SUFFIXES:
                allowed = ", ".join(sorted(ALLOWED_SUFFIXES))
                lines.append(f"✗ {raw}: 后缀不在白名单（{allowed}）")
                continue
            size = src.stat().st_size
            if size > MAX_BYTES:
                lines.append(f"✗ {raw}: {size} 字节超过上限 {MAX_BYTES}")
                continue
            target = _resolve_with_conflict(self.root / src.name)
            shutil.copy2(src, target)
            lines.append(f"✓ {raw} → {_display(target)}")
        return "\n".join(lines)


class StageFilesTool(Tool):
    """把 reports/ 或 uploads/ 下的文件复制到 wiki/staging/，喂给 wiki_curator。

    跨 agent 数据传递的「归档闸口」：ResearchAgent 产出（reports/）和用户原料
    （uploads/）在此汇聚成 staging/——这是 WikiAgent.read_source 唯一允许读的目录。
    """

    DEFAULT_WIKI = "wiki"
    DEFAULT_REPORTS = "reports"
    DEFAULT_UPLOADS = "uploads"
    ALLOWED_PREFIXES = ("reports/", "uploads/")

    spec = ToolSpec(
        name="stage_files",
        description=(
            "把 reports/ 或 uploads/ 下的文件复制到 wiki/staging/。派 wiki_curator 前"
            "必须先调本工具——wiki_curator 只能读 staging/。paths 是工作区相对路径，"
            "必须以 reports/ 或 uploads/ 开头。返回 staging 内路径列表（用作下游 prompt 的引用）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "工作区相对路径列表（reports/... 或 uploads/...）",
                }
            },
            "required": ["paths"],
        },
    )

    def __init__(
        self,
        wiki_root: Optional[Path] = None,
        reports_root: Optional[Path] = None,
        uploads_root: Optional[Path] = None,
    ):
        wiki = Path(wiki_root) if wiki_root else Path(self.DEFAULT_WIKI)
        self.wiki_root = wiki.resolve()
        self.staging = self.wiki_root / "staging"
        self.reports_root = (
            Path(reports_root) if reports_root else Path(self.DEFAULT_REPORTS)
        ).resolve()
        self.uploads_root = (
            Path(uploads_root) if uploads_root else Path(self.DEFAULT_UPLOADS)
        ).resolve()

    async def execute(self, paths: List[str]) -> str:
        if not paths:
            return "Error: paths 不能为空"
        self.staging.mkdir(parents=True, exist_ok=True)
        lines: List[str] = []
        for raw in paths:
            if not any(raw.startswith(p) for p in self.ALLOWED_PREFIXES):
                lines.append(f"✗ {raw}: 只允许 reports/ 或 uploads/ 路径")
                continue
            try:
                src = Path(raw).resolve()
            except (OSError, RuntimeError) as e:
                lines.append(f"✗ {raw}: 路径解析失败 ({e})")
                continue
            # 沙箱：解析后必须仍在 reports_root 或 uploads_root 之下
            in_reports = self._under(src, self.reports_root)
            in_uploads = self._under(src, self.uploads_root)
            if not (in_reports or in_uploads):
                lines.append(f"✗ {raw}: 解析后越界（不在 reports/ 或 uploads/ 之下）")
                continue
            if not src.is_file():
                lines.append(f"✗ {raw}: 文件不存在")
                continue
            size = src.stat().st_size
            if size > MAX_BYTES:
                lines.append(f"✗ {raw}: {size} 字节超过上限 {MAX_BYTES}")
                continue
            target = _resolve_with_conflict(self.staging / src.name)
            shutil.copy2(src, target)
            lines.append(f"✓ {raw} → staging/{target.name}")
        return "\n".join(lines)

    @staticmethod
    def _under(p: Path, root: Path) -> bool:
        try:
            p.relative_to(root)
            return True
        except ValueError:
            return False
