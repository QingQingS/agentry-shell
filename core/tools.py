"""
WikiAgent 工具层 + ./wiki/ 沙箱（设计见 wiki-agent开发.md 第八节）。

设计要点：
  - 显式 class-based：每个工具一个类，JSON schema 手写，契约可见（不用装饰器推断）。
  - 沙箱集中在 FileTool._resolve 一处：所有路径解析后必须落在 wiki 根内，
    挡路径穿越 / 绝对路径 / 符号链接逃逸。
  - 错误处理：工具方法对越界 raise SandboxViolation；其余文件错误由工具自行
    返回 observation 字符串。ToolRegistry.execute 在边界 catch 一切，永远返回
    字符串（成功结果或 "Error: ..."），ReAct 循环因此永不被工具异常打断。
  - 工具永不向循环抛异常 → 唯一能停循环的是 LLM 不再发 tool_call 或 MAX_STEPS。

可观测性（每步 log 事件）由 Step D 的 ReAct 循环依据 execute 返回值产出，
本层保持纯粹、可离线单测。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List

from core.llm.base import ToolCall, ToolSpec
from core.wiki_index import INDEX_FILENAME, collect_pages


class SandboxViolation(Exception):
    """路径解析后落在 wiki 沙箱之外。由 _resolve 抛出，registry 边界捕获。"""

    def __init__(self, path: str):
        self.path = path
        super().__init__(f"路径越界: {path}")


class Tool(ABC):
    """工具基类：spec 向 LLM 广告，execute 吃解析好的参数、吐 observation 字符串。"""

    spec: ToolSpec

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        ...


class FileTool(Tool):
    """受沙箱约束的文件类工具基类，持有 wiki 根目录。"""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def _resolve(self, path: str) -> Path:
        """把相对/绝对路径规范化并校验在沙箱内；越界则抛 SandboxViolation。"""
        p = (self.root / path).resolve()  # 摊平 .. 与符号链接；绝对路径会丢弃左侧
        if not p.is_relative_to(self.root):
            raise SandboxViolation(path)
        return p


class ReadFileTool(FileTool):
    spec = ToolSpec(
        name="read_file",
        description="读取 wiki 内某个页面的完整内容。path 是相对 wiki 根的路径，如 AI/transformer.md 或 index.md。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对 wiki 根的页面路径"}
            },
            "required": ["path"],
        },
    )

    async def execute(self, path: str) -> str:
        p = self._resolve(path)
        if not p.is_file():
            return f"Error: 文件不存在: {path}"
        return p.read_text(encoding="utf-8")


class WriteFileTool(FileTool):
    spec = ToolSpec(
        name="write_file",
        description=(
            "整篇覆盖写入 wiki 内的一个 .md 页面（不存在则新建，父目录自动创建）。"
            "path 相对 wiki 根，如 AI/transformer.md；content 是页面完整内容。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对 wiki 根的页面路径，须以 .md 结尾"},
                "content": {"type": "string", "description": "页面完整内容（整篇覆盖）"},
            },
            "required": ["path", "content"],
        },
    )

    async def execute(self, path: str, content: str) -> str:
        p = self._resolve(path)  # 沙箱优先校验
        if p.suffix != ".md":
            return f"Error: 只允许写入 .md 文件，拒绝: {path}"
        if p == self.root / INDEX_FILENAME:
            return "Error: index.md 由系统自动维护，无需也不允许手动写入；只要写好各页 frontmatter 即可。"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        rel = p.relative_to(self.root)
        return f"已写入 {rel}（{len(content)} 字符）"


class ListFilesTool(FileTool):
    spec = ToolSpec(
        name="list_files",
        description=(
            "递归列出 wiki 内所有 .md 页面（相对 wiki 根的路径）。"
            "可选 subdir 限定子目录，留空则列全部。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "subdir": {"type": "string", "description": "可选，限定的子目录；留空列出全部"}
            },
            "required": [],
        },
    )

    STAGING_PREFIX = "staging/"

    async def execute(self, subdir: str = "") -> str:
        base = self._resolve(subdir) if subdir else self.root
        if not base.exists():
            return f"Error: 目录不存在: {subdir}"
        if not base.is_dir():
            return f"Error: 不是目录: {subdir}"
        rels = sorted(str(f.relative_to(self.root)) for f in base.rglob("*.md"))
        # 默认列举排除 staging/（待归档原料，尚未策展），避免把它当成已策展页面；
        # 但显式 list_files('staging') 时不过滤，curator 借此发现待归档文件。
        listing_staging = base != self.root and base.relative_to(self.root).parts[:1] == (
            self.STAGING_PREFIX.rstrip("/"),
        )
        if not listing_staging:
            rels = [r for r in rels if not r.startswith(self.STAGING_PREFIX)]
        if not rels:
            return "(wiki 内暂无 .md 页面)"
        return "\n".join(rels)


class ReadSourceTool(FileTool):
    """读取 wiki/staging/ 下的待归档原文（受 wiki 沙箱保护 + 强制 staging/ 子目录）。

    跨 agent 的「待归档原料」走文件系统传递（搬运由 wiki_curator 派发时的 pre-hook
    stage_wiki_inputs 完成，幂等，Coordinator 不经手）：
      - 用户外部文件 → uploads/ → (pre-hook) → wiki/staging/ → 本工具
      - ResearchAgent 产出 → reports/ → (pre-hook) → wiki/staging/ → 本工具
    任何原料都必须先进 staging/，本工具是 WikiAgent 看到「外界」的唯一入口；
    沙箱原则保持不破。

    限制：
    - path 必须以 staging/ 开头（沙箱外的兄弟目录会被拒绝）
    - 后缀白名单（.md/.markdown/.txt/.rst）—— stage 时也应已校验，这里防御性兜底
    - 单文件大小上限 MAX_BYTES
    """

    MAX_BYTES = 1024 * 1024  # 1 MiB
    ALLOWED_SUFFIXES = {".md", ".markdown", ".txt", ".rst"}
    STAGING_PREFIX = "staging/"

    spec = ToolSpec(
        name="read_source",
        description=(
            "读取 wiki/staging/ 下的待归档文本文件（只读）。"
            "path 相对 wiki 根，必须以 staging/ 开头（如 staging/foo.md）。"
            "配合 list_files('staging') 使用，可看到当前 staging 里有哪些待归档原料。"
            "支持 .md/.markdown/.txt/.rst；单文件上限 1 MiB。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对 wiki 根的路径，必须以 staging/ 开头",
                }
            },
            "required": ["path"],
        },
    )

    async def execute(self, path: str) -> str:
        if not path.startswith(self.STAGING_PREFIX):
            return f"Error: read_source 只允许读 staging/ 下的文件，拒绝: {path}"
        p = self._resolve(path)  # 沙箱校验：落入 wiki_root 内
        if not p.is_file():
            return f"Error: 文件不存在: {path}"
        if p.suffix.lower() not in self.ALLOWED_SUFFIXES:
            allowed = ", ".join(sorted(self.ALLOWED_SUFFIXES))
            return f"Error: 只允许读取文本文件（{allowed}），拒绝: {path}"
        size = p.stat().st_size
        if size > self.MAX_BYTES:
            return f"Error: 文件 {path} 大小 {size} 字节超过上限 {self.MAX_BYTES}"
        return p.read_text(encoding="utf-8")


class WikiSearchTool(FileTool):
    """只读：在已归档的 wiki 知识库里按关键词检索页面（research→curate→reuse 的 reuse 一环）。

    刻意保持简单（无排序模型 / 向量化）：对每个已策展页面做 query 词与
    标题+描述+实体+正文的词重叠打分，返回命中页面的 path/标题/片段。
    仅检索已策展页面——staging/ 与 index.md 由 collect_pages 排除（呼应 T6），
    所以 reuse 不会命中待归档垃圾。Coordinator 在 chat 路径据此引用已有知识。
    """

    spec = ToolSpec(
        name="wiki_search",
        description=(
            "在已归档的本地 wiki 知识库里按关键词检索页面（只读）。返回命中页面的 "
            "path/标题/片段，便于在回答里复用并引用已沉淀的知识。"
            "只检索已策展页面（不含 staging/、index.md）。"
            "命中后可用 read_file(path) 读取该页全文再作答。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词"},
                "max_results": {"type": "integer", "description": "返回数量上限", "default": 5},
            },
            "required": ["query"],
        },
    )

    SNIPPET_LEN = 160

    async def execute(self, query: str, max_results: int = 5) -> str:
        q = set(re.findall(r"\w+", (query or "").lower()))
        if not q:
            return "(检索词为空)"
        scored: List[tuple] = []
        for pg in collect_pages(self.root):
            try:
                body = (self.root / pg.path).read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 — 读不到的页跳过，不打断检索
                body = ""
            hay = f"{pg.title}\n{pg.description}\n{' '.join(pg.entities)}\n{body}".lower()
            overlap = len(q & set(re.findall(r"\w+", hay)))
            if overlap:
                scored.append((overlap, pg, body))
        if not scored:
            return "(wiki 内未检索到相关页面)"
        scored.sort(key=lambda x: x[0], reverse=True)
        blocks = []
        for _, pg, body in scored[:max_results]:
            snippet = re.sub(r"\s+", " ", body).strip()[: self.SNIPPET_LEN]
            blocks.append(f"path: {pg.path}\n标题: {pg.title}\n片段: {snippet}")
        return "\n\n".join(blocks)


class ToolRegistry:
    """按 name 持有工具；对 ReAct 循环暴露 specs() 与 execute(call)。"""

    def __init__(self, tools: List[Tool]):
        self._tools = {t.spec.name: t for t in tools}

    def specs(self) -> List[ToolSpec]:
        return [t.spec for t in self._tools.values()]

    async def execute(self, call: ToolCall) -> str:
        """分发并执行；任何失败都收敛成 observation 字符串，绝不向上抛。"""
        tool = self._tools.get(call.name)
        if tool is None:
            return f"Error: 未知工具: {call.name}"
        try:
            return await tool.execute(**call.arguments)
        except SandboxViolation as e:
            return f"Error: 路径 {e.path} 在 wiki 沙箱外，已拒绝访问"
        except TypeError as e:
            return f"Error: 工具 {call.name} 参数不匹配: {e}"
        except Exception as e:  # noqa: BLE001 — 兜底，保证循环不被工具异常打断
            return f"Error: 工具 {call.name} 执行失败: {type(e).__name__}: {e}"


WIKI_INDEX_SKELETON = """# Wiki Index
*最近更新：尚未有内容*

> 本文件是 wiki 的目录脊梁：每个页面一条记录，按 category 分组。
> 每次 ingest 时更新此处。
"""


def build_wiki_registry(wiki_root: Path) -> ToolRegistry:
    """
    构造 wiki 工具注册表，并完成冷启动：
    确保 wiki 根目录存在，若无 index.md 则种入骨架（免得 list/read 返回令人困惑的空）。
    """
    wiki_root = Path(wiki_root)
    wiki_root.mkdir(parents=True, exist_ok=True)
    index = wiki_root / "index.md"
    if not index.exists():
        index.write_text(WIKI_INDEX_SKELETON, encoding="utf-8")
    root = wiki_root.resolve()
    return ToolRegistry(
        [ReadFileTool(root), WriteFileTool(root), ListFilesTool(root), ReadSourceTool(root)]
    )
