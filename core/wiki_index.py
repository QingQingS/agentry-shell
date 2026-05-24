"""
wiki 目录（index.md）的代码侧投影（Opt 2，设计见 wiki_agent优化.md 第五节）。

index.md 不再由 LLM 读写，而是各页 frontmatter 的**确定性投影**：
  - build_catalog(root)    把各页 frontmatter 摘成精简目录文本，注入 WikiAgent 开局 prompt
  - regenerate_index(root) 把同一投影写回 index.md（ReAct 循环结束后由 agent 调用）
两者共享解析与渲染逻辑，index.md 因此永不与页面漂移。

容错：受控但终究由 LLM 生成的 frontmatter，单页解析失败不崩——降级为仅含
title（取自 frontmatter 或文件名）的记录，该页仍出现在目录里。

frontmatter 是简单平铺的 key: value（外加 entities 的内联列表 [a, b, c]）。
手写解析而非 pyyaml：partition(":") 只切第一个冒号，描述里的冒号原样留在值里；
pyyaml 反而会因未加引号的冒号报错。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

INDEX_FILENAME = "index.md"


@dataclass
class PageMeta:
    path: str            # 相对 root，如 AI/rag.md
    title: str
    category: str
    description: str
    entities: List[str]
    sources: str
    updated: str


def _extract_frontmatter(text: str) -> Optional[str]:
    """取首行 --- 与下一个 --- 之间的块；不符合则返回 None。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i])
    return None


def _parse_entities(raw: str) -> List[str]:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [e.strip() for e in raw.split(",") if e.strip()]


def _parse_page(root: Path, p: Path) -> PageMeta:
    rel = str(p.relative_to(root))
    fields: Dict[str, str] = {}
    fm = _extract_frontmatter(p.read_text(encoding="utf-8"))
    if fm:
        for line in fm.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")   # 只切第一个冒号
            fields[key.strip()] = val.strip()
    return PageMeta(
        path=rel,
        title=fields.get("title") or p.stem,
        category=fields.get("category") or "未分类",
        description=fields.get("description", ""),
        entities=_parse_entities(fields.get("entities", "")),
        sources=fields.get("sources", ""),
        updated=fields.get("updated", ""),
    )


def collect_pages(root: Path) -> List[PageMeta]:
    """收集 root 下除 index.md 外所有 .md 页面的元数据（路径序，确定性）。"""
    root = Path(root)
    pages: List[PageMeta] = []
    for p in sorted(root.rglob("*.md")):
        if p.name == INDEX_FILENAME:
            continue
        try:
            pages.append(_parse_page(root, p))
        except Exception:  # noqa: BLE001 — 极端容错：连读都失败仍留一条文件名记录
            pages.append(PageMeta(
                path=str(p.relative_to(root)), title=p.stem, category="未分类",
                description="", entities=[], sources="", updated="",
            ))
    return pages


def _render(pages: List[PageMeta], today: Optional[str]) -> str:
    out = ["# Wiki Index"]
    if today:
        out.append(f"*最近更新：{today}*")
    if not pages:
        out.append("\n> 暂无页面。")
        return "\n".join(out) + "\n"
    by_cat: Dict[str, List[PageMeta]] = {}
    for pg in pages:
        by_cat.setdefault(pg.category, []).append(pg)
    for cat in sorted(by_cat):
        out.append(f"\n## {cat}")
        for pg in by_cat[cat]:
            line = f"- [{pg.title}]({pg.path}) — {pg.description}"
            extras = []
            if pg.entities:
                extras.append("实体: " + ", ".join(pg.entities))
            if pg.sources:
                extras.append(f"来源: {pg.sources}")
            if pg.updated:
                extras.append(pg.updated)
            if extras:
                line += " | " + " | ".join(extras)
            out.append(line)
    return "\n".join(out) + "\n"


def build_catalog(root: Path) -> str:
    """注入 prompt 的目录文本（不带"最近更新"日期行——定向不需要日期）。"""
    return _render(collect_pages(root), today=None)


def regenerate_index(root: Path, today: str) -> str:
    """从各页 frontmatter 重生成 index.md 并写回；返回写入的内容。"""
    root = Path(root)
    content = _render(collect_pages(root), today=today)
    (root / INDEX_FILENAME).write_text(content, encoding="utf-8")
    return content
