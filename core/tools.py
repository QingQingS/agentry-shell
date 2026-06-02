"""
工具层 + 声明式文件权限（Scope）。

设计要点：
  - 显式 class-based：每个工具一个类，JSON schema 手写，契约可见（不用装饰器推断）。
  - 权限与机制解耦：文件工具不再各自手写路径安全，而是持有一个 Scope（见 core/scope.py），
    所有越界/只读/后缀/子目录/大小校验都收敛到 Scope.resolve 一处。工具只表达「读/写/列」
    这件事本身，能读写哪里由注入的 Scope 声明。
  - 契约可注入：name/description 是构造参数（含通用默认值），同一个工具类可被不同 agent
    复用而广告各自的口径（如 read_file 在 WikiAgent 说「wiki 页面」、在 Coordinator 说「工作区文件」）。
  - 错误处理：策略违规 raise ScopeViolation；其余文件错误由工具自行返回 observation 字符串。
    ToolRegistry.execute 在边界 catch 一切，永远返回字符串，ReAct 循环因此永不被工具异常打断。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

from core.llm.base import ToolCall, ToolSpec
from core.scope import Scope, ScopeViolation
from core.wiki_index import INDEX_FILENAME, collect_pages

# staging 待归档原料的策略常量（read_source 的 Scope 用）。
STAGING_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst"})
STAGING_MAX_BYTES = 1024 * 1024  # 1 MiB

_PATH_PARAM = {
    "type": "object",
    "properties": {"path": {"type": "string", "description": "目标文件路径（相对该工具的根）"}},
    "required": ["path"],
}


class Tool(ABC):
    """工具基类：spec 向 LLM 广告，execute 吃解析好的参数、吐 observation 字符串。"""

    spec: ToolSpec

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        ...


class FileTool(Tool):
    """受 Scope 约束的文件类工具基类：持有一个 Scope，权限全部委托给它。"""

    def __init__(self, scope: Scope):
        self.scope = scope


class ReadFileTool(FileTool):
    """读取 scope 内某个文件的完整内容（只读）。

    同一个类服务多种「读」：普通文件读（read_file）、受限只读原料（read_source，传 staging
    scope 即可）——差别只在注入的 Scope 与广告口径，逻辑零分叉。
    """

    DEFAULT_DESC = "读取指定文件的完整内容（只读，path 相对该工具的根目录）。"

    def __init__(self, scope: Scope, *, name: str = "read_file", description: Optional[str] = None):
        super().__init__(scope)
        self.spec = ToolSpec(
            name=name,
            description=description or self.DEFAULT_DESC,
            parameters=_PATH_PARAM,
        )

    async def execute(self, path: str) -> str:
        p = self.scope.resolve(path)
        if not p.is_file():
            return f"Error: 文件不存在: {path}"
        size = p.stat().st_size
        if not self.scope.within_size(size):
            return f"Error: 文件 {path} 大小 {size} 字节超过上限 {self.scope.max_bytes}"
        return p.read_text(encoding="utf-8")


class WriteFileTool(FileTool):
    """整篇覆盖写入 scope 内的一个文件（不存在则新建，父目录自动创建）。

    可写性/后缀白名单/禁止名（如 index.md）都由注入的 Scope 声明，本工具不内嵌这些策略。
    """

    DEFAULT_DESC = (
        "整篇覆盖写入指定文件（不存在则新建，父目录自动创建）。"
        "path 相对该工具的根目录；content 是文件完整内容。"
    )

    def __init__(self, scope: Scope, *, name: str = "write_file", description: Optional[str] = None):
        super().__init__(scope)
        self.spec = ToolSpec(
            name=name,
            description=description or self.DEFAULT_DESC,
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径（相对该工具的根）"},
                    "content": {"type": "string", "description": "文件完整内容（整篇覆盖）"},
                },
                "required": ["path", "content"],
            },
        )

    async def execute(self, path: str, content: str) -> str:
        p = self.scope.resolve(path, for_write=True)  # 越界/只读/后缀/禁止名一处校验
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        rel = p.relative_to(self.scope.root)
        return f"已写入 {rel}（{len(content)} 字符）"


class ListFilesTool(FileTool):
    """递归列出 scope 内所有 .md 页面（相对根的路径）。wiki 专用：默认排除 staging/。"""

    DEFAULT_DESC = (
        "递归列出根目录内所有 .md 页面（相对根的路径）。"
        "可选 subdir 限定子目录，留空则列全部。"
    )
    STAGING_PREFIX = "staging/"

    def __init__(self, scope: Scope, *, name: str = "list_files", description: Optional[str] = None):
        super().__init__(scope)
        self.spec = ToolSpec(
            name=name,
            description=description or self.DEFAULT_DESC,
            parameters={
                "type": "object",
                "properties": {
                    "subdir": {"type": "string", "description": "可选，限定的子目录；留空列出全部"}
                },
                "required": [],
            },
        )

    async def execute(self, subdir: str = "") -> str:
        root = self.scope.root
        base = self.scope.resolve(subdir) if subdir else root
        if not base.exists():
            return f"Error: 目录不存在: {subdir}"
        if not base.is_dir():
            return f"Error: 不是目录: {subdir}"
        rels = sorted(str(f.relative_to(root)) for f in base.rglob("*.md"))
        # 默认列举排除 staging/（待归档原料，尚未策展）；但显式 list_files('staging') 时不过滤，
        # curator 借此发现待归档文件。
        listing_staging = base != root and base.relative_to(root).parts[:1] == (
            self.STAGING_PREFIX.rstrip("/"),
        )
        if not listing_staging:
            rels = [r for r in rels if not r.startswith(self.STAGING_PREFIX)]
        if not rels:
            return "(根目录内暂无 .md 页面)"
        return "\n".join(rels)


class WikiSearchTool(FileTool):
    """只读：在已归档的 wiki 知识库里按关键词检索页面（research→curate→reuse 的 reuse 一环）。

    刻意保持简单（无排序模型 / 向量化）：对每个已策展页面做 query 词与标题+描述+实体+正文的
    词重叠打分，返回命中页面的 path/标题/片段。仅检索已策展页面（collect_pages 排除 staging/
    与 index.md）。检索/读取边界 = scope.root（不被放开）。
    """

    SNIPPET_LEN = 160

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

    def __init__(self, scope: Scope, path_prefix: str = ""):
        # path_prefix：拼在命中 path 前面，使返回的路径与调用方的 read_file 根口径一致。
        # Coordinator 的 read_file 根是项目根，故传 "wiki/"，让 reuse→read_file 接得上；
        # 留空则按 wiki 根相对返回。
        super().__init__(scope)
        self.path_prefix = path_prefix

    async def execute(self, query: str, max_results: int = 5) -> str:
        q = set(re.findall(r"\w+", (query or "").lower()))
        if not q:
            return "(检索词为空)"
        root = self.scope.root
        scored: List[tuple] = []
        for pg in collect_pages(root):
            try:
                body = (root / pg.path).read_text(encoding="utf-8")
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
            blocks.append(f"path: {self.path_prefix}{pg.path}\n标题: {pg.title}\n片段: {snippet}")
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
        except ScopeViolation as e:
            return f"Error: {e}"
        except TypeError as e:
            return f"Error: 工具 {call.name} 参数不匹配: {e}"
        except Exception as e:  # noqa: BLE001 — 兜底，保证循环不被工具异常打断
            return f"Error: 工具 {call.name} 执行失败: {type(e).__name__}: {e}"


WIKI_INDEX_SKELETON = """# Wiki Index
*最近更新：尚未有内容*

> 本文件是 wiki 的目录脊梁：每个页面一条记录，按 category 分组。
> 每次 ingest 时更新此处。
"""

# WikiAgent 的工具广告口径（沿用历史措辞，保持其 LLM 契约不变）。
_WIKI_READ_DESC = "读取 wiki 内某个页面的完整内容。path 是相对 wiki 根的路径，如 AI/transformer.md 或 index.md。"
_WIKI_WRITE_DESC = (
    "整篇覆盖写入 wiki 内的一个 .md 页面（不存在则新建，父目录自动创建）。"
    "path 相对 wiki 根，如 AI/transformer.md；content 是页面完整内容。"
)
_WIKI_LIST_DESC = (
    "递归列出 wiki 内所有 .md 页面（相对 wiki 根的路径）。"
    "可选 subdir 限定子目录，留空则列全部。"
)
_READ_SOURCE_DESC = (
    "读取 wiki/staging/ 下的待归档文本文件（只读）。"
    "path 相对 wiki 根，必须以 staging/ 开头（如 staging/foo.md）。"
    "配合 list_files('staging') 使用，可看到当前 staging 里有哪些待归档原料。"
    "支持 .md/.markdown/.txt/.rst；单文件上限 1 MiB。"
)


def build_wiki_registry(wiki_root: Path) -> ToolRegistry:
    """
    构造 wiki 工具注册表，并完成冷启动：
    确保 wiki 根目录存在，若无 index.md 则种入骨架（免得 list/read 返回令人困惑的空）。

    在一处集中声明 WikiAgent 的文件权限：
      - read/list：整个 wiki，只读、不限后缀（浏览/读取看得全）。
      - write：仅 .md，且禁写 index.md（由系统维护）。
      - staging：限定 staging/ 子目录、文本后缀白名单、单文件 1 MiB（待归档原料只读入口）。
    """
    wiki_root = Path(wiki_root)
    wiki_root.mkdir(parents=True, exist_ok=True)
    index = wiki_root / "index.md"
    if not index.exists():
        index.write_text(WIKI_INDEX_SKELETON, encoding="utf-8")

    read_scope = Scope(root=wiki_root)
    write_scope = Scope(
        root=wiki_root,
        writable=True,
        allowed_suffixes=frozenset({".md"}),
        denied_names=frozenset({INDEX_FILENAME}),
    )
    staging_scope = Scope(
        root=wiki_root,
        subdir="staging/",
        allowed_suffixes=STAGING_SUFFIXES,
        max_bytes=STAGING_MAX_BYTES,
    )
    return ToolRegistry([
        ReadFileTool(read_scope, name="read_file", description=_WIKI_READ_DESC),
        WriteFileTool(write_scope, name="write_file", description=_WIKI_WRITE_DESC),
        ListFilesTool(read_scope, name="list_files", description=_WIKI_LIST_DESC),
        ReadFileTool(staging_scope, name="read_source", description=_READ_SOURCE_DESC),
    ])
