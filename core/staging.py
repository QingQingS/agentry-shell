"""
Coordinator 端的工作区搬运工具 + spoke 入参 pre-hook（v2 hub-and-spoke 数据流的「闸口」）。

跨 agent 数据通过文件系统传递，本模块定义两条受控通道：

  外部 path ──import_files──► uploads/  ─┐
                                          ├──stage_wiki_inputs──► wiki/staging/ ──read_source──► WikiAgent
        ResearchAgent.save_report ──► reports/ ─┘

- import_files：唯一允许接受「外部任意 path」的工具——Coordinator 用它把用户提到的
  外部文件收口到 uploads/，避免下游 spoke 直接读外部路径破坏沙箱。它仍是 Coordinator
  工具表里的工具（外部入口必须由 LLM 显式触发）。
- staging 不再是 Coordinator 的工具。它降级成 wiki_curator 派发的内部 pre-hook
  （stage_wiki_inputs）：Coordinator 只在 dispatch_agent(wiki_curator, files=[...]) 里给出
  reports//uploads/ 路径，搬运由 hook 唯一一处幂等完成，Coordinator 看不到也碰不到 staging/。
  这从「能力层面」消除了旧 StageFilesTool 的重复写入 bug（失败重试→换时间戳名→staging 堆积
  →下次更易超时的雪崩）：同一源永远拍平成同一目标名，同内容幂等跳过，同名异内容报错。

设计约束：
- ImportFilesTool 继承 core/tools.py 的 Tool ABC，可直接放进 Coordinator 的 ToolRegistry。
- stage_one 失败一律 raise StageError；stage_wiki_inputs 把它转成错误字符串短路返回，
  dispatch 端再转成和 spoke 真失败同形的 error observation（循环不被异常打断）。
- 默认根目录走 cwd 相对路径（./uploads, ./reports, ./wiki/staging），测试时通过
  构造参数注入或 chdir 隔离。
"""

from __future__ import annotations

import filecmp
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
    """若目标存在则追加时间戳后缀，避免覆盖（归档性存储 uploads/ 用，防覆盖是对的）。"""
    if not target.exists():
        return target
    return target.with_name(f"{target.stem}-{_ts_suffix()}{target.suffix}")


class ImportFilesTool(Tool):
    """把用户提到的外部文件复制到 uploads/，作为后续归档的源头。

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


# ---- staging：wiki_curator 的 pre-hook（不再是 Coordinator 的工具）----

ALLOWED_PREFIXES = ("reports/", "uploads/")
DEFAULT_WIKI = "wiki"
DEFAULT_REPORTS = "reports"
DEFAULT_UPLOADS = "uploads"


class StageError(Exception):
    """staging 搬运的受控失败（前缀非法 / 源不存在 / 越界 / 超限 / 拍平命名碰撞）。"""


def _under(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def stage_one(
    src: str,
    *,
    reports_root: Path,
    uploads_root: Path,
    staging_root: Path,
) -> Path:
    """把单个工作区文件幂等地搬进 staging/，返回 staging 内的目标路径。

    - src 必须以 reports/ 或 uploads/ 开头，且解析后仍在对应根之下（沙箱）。
    - 拍平命名：reports/a/b.md → reports__a__b.md。源路径编码进目标名，于是：
        · 同一源永远映射到同一目标名（旧 bug 的时间戳变体不再可能）；
        · 不同源不会撞名（除非确实同一路径）。
    - 幂等：目标已存在且内容相同 → 跳过 copy，直接返回（重试安全）。
    - 同名异内容 → raise StageError（机械故障，报错而非覆盖 / 加时间戳）。
    失败一律 raise StageError（调用方转成 observation）。
    """
    if not any(src.startswith(p) for p in ALLOWED_PREFIXES):
        raise StageError(f"非法源前缀（只允许 reports/ 或 uploads/）：{src}")
    try:
        resolved = Path(src).resolve()
    except (OSError, RuntimeError) as e:
        raise StageError(f"路径解析失败（{src}）：{e}")
    if not (_under(resolved, reports_root) or _under(resolved, uploads_root)):
        raise StageError(f"解析后越界（不在 reports/ 或 uploads/ 之下）：{src}")
    if not resolved.is_file():
        raise StageError(f"源不存在：{src}")
    size = resolved.stat().st_size
    if size > MAX_BYTES:
        raise StageError(f"{src}: {size} 字节超过上限 {MAX_BYTES}")

    staging_root.mkdir(parents=True, exist_ok=True)
    dest = staging_root / src.replace("/", "__")
    if dest.exists():
        if filecmp.cmp(dest, resolved, shallow=False):
            return dest  # 幂等：同内容已在 staging，重试无副作用
        raise StageError(f"拍平命名碰撞：{src} 与已暂存文件同名但内容不同")
    shutil.copy2(resolved, dest)
    return dest


def stage_wiki_inputs(payload: dict) -> Optional[str]:
    """wiki_curator 的 pre-hook：把 payload['files'] 搬进 staging/ 并就地改写成 staging 内文件名。

    hook 契约（dispatch 通用遍历）：原地改写 payload；成功返回 None；要短路返回错误字符串。
    改写后 payload['files'] 是 staging 相对文件名（read_source 的 path 即以 staging 为根）。
    根目录走 cwd 相对默认（./reports ./uploads ./wiki/staging），与历史 StageFilesTool 一致。
    """
    files = payload.get("files") or []
    if not files:
        return None  # 无文件要搬（原文可能已在 prompt/context 里），hook 无操作

    reports_root = Path(DEFAULT_REPORTS).resolve()
    uploads_root = Path(DEFAULT_UPLOADS).resolve()
    staging_root = (Path(DEFAULT_WIKI) / "staging").resolve()

    staged: List[str] = []
    for src in files:
        try:
            dest = stage_one(
                src,
                reports_root=reports_root,
                uploads_root=uploads_root,
                staging_root=staging_root,
            )
        except StageError as e:
            return f"staging 失败：{e}"
        staged.append(dest.name)  # read_source 以 staging 为根，传裸文件名
    payload["files"] = staged
    return None
