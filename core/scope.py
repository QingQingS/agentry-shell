"""
文件权限原语：Scope —— 一个 agent 对文件系统的受控视图（根 + 策略）。

为什么单独成模块：
  - 过去每个文件工具各自手写路径安全（FileTool 一套沙箱、SaveReportTool/ImportFilesTool
    各自重写 ../后缀校验），策略散落、易漏、难审计。Scope 把「能读哪/能写哪/什么后缀/
    限不限子目录/大小上限/禁止文件」收进一个声明式值对象，并提供**唯一校验入口** resolve()。
  - 零依赖（只用 pathlib），core/tools.py、core/staging.py、agents/research_tools.py 都能
    引用而不产生循环依赖。

用法：每个 agent 在组装工具时声明自己的 Scope（通常读/列一个宽 scope、写一个窄 scope），
工具只管调 scope.resolve()，不再自带策略。新增 agent = 写一行 Scope(...)，复用同一批工具。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Optional


class ScopeViolation(Exception):
    """路径触犯了 Scope 策略（越界/只读/后缀/子目录/禁止名）。

    由 Scope.resolve 抛出，统一在 ToolRegistry.execute 边界捕获并转成 observation 字符串，
    ReAct 循环因此永不被工具异常打断。reason 面向 LLM，需可读、可据此纠正。
    """

    def __init__(self, path: str, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"{reason}: {path}")


@dataclass(frozen=True)
class Scope:
    """一个受控的文件系统视图。所有文件工具的权限都经由此处校验，策略只在此声明一次。

    字段即策略：
      - root             沙箱根；所有路径解析后必须落在其内（防穿越/绝对路径/符号链接逃逸）。
      - writable         是否允许写；只读 scope 上的写操作会被拒。
      - subdir           非空则进一步限定只能访问 root/subdir 下（如 "staging/"）。
      - allowed_suffixes None=不限；否则只允许这些后缀的**文件**（目录/列举请用不设后缀的 scope）。
      - denied_names     相对 root 的禁止路径集合（如 "index.md" 由系统维护）。
      - max_bytes        读/写字节上限；None=不限。由工具用 within_size 自行校验大小。
    """

    root: Path
    writable: bool = False
    subdir: str = ""
    allowed_suffixes: Optional[FrozenSet[str]] = None
    denied_names: FrozenSet[str] = frozenset()
    max_bytes: Optional[int] = None

    def __post_init__(self) -> None:
        # frozen dataclass：用 object.__setattr__ 规范化 root（摊平为绝对路径，幂等）。
        object.__setattr__(self, "root", Path(self.root).resolve())

    def resolve(self, path: str, *, for_write: bool = False) -> Path:
        """把相对/绝对路径规范化并逐条校验策略；任一不过抛 ScopeViolation。

        校验顺序固定：越界 → 子目录 → 可写性 → 后缀 → 禁止名。返回校验通过的绝对路径。
        """
        p = (self.root / path).resolve()  # 摊平 .. 与符号链接；绝对路径会丢弃左侧
        if not p.is_relative_to(self.root):
            raise ScopeViolation(path, "路径越界（沙箱外）")
        if self.subdir:
            base = (self.root / self.subdir).resolve()
            if not p.is_relative_to(base):
                raise ScopeViolation(path, f"只允许访问 {self.subdir} 下的文件")
        if for_write and not self.writable:
            raise ScopeViolation(path, "该范围为只读，拒绝写入")
        if self.allowed_suffixes is not None and p.suffix.lower() not in self.allowed_suffixes:
            allowed = ", ".join(sorted(self.allowed_suffixes))
            raise ScopeViolation(path, f"只允许 {allowed} 文件")
        rel = p.relative_to(self.root).as_posix()
        if rel in self.denied_names:
            raise ScopeViolation(path, "该文件由系统维护，禁止手动访问")
        return p

    def within_size(self, n: int) -> bool:
        """字节数是否在上限内（max_bytes=None 视为不限）。"""
        return self.max_bytes is None or n <= self.max_bytes
