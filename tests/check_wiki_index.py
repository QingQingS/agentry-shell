"""
Step 2a 验证：core/wiki_index.py —— index.md 的代码侧投影。

离线、无 API。验证：
  - frontmatter 解析（含描述里带冒号、entities 内联列表）
  - build_catalog 按类别分组、含描述与实体
  - regenerate_index 落盘且内容一致；冷启动空 wiki 产出合法空 index
  - 容错：坏 frontmatter 的页面不崩、仍出现在目录里

跑法：
  PY=/usr/local/Caskroom/miniforge/base/envs/claude-deepseek/bin/python
  $PY tests/check_wiki_index.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.tools import ListFilesTool
from core.wiki_index import build_catalog, collect_pages, regenerate_index


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


PAGE_OK = """\
---
title: 检索增强生成（RAG）
category: AI
description: 结合外部检索与 LLM 生成的技术：缓解知识陈旧与幻觉
created: 2026-05-24
updated: 2026-05-24
sources: 1
entities: [RAG, Embedding Model, Vector Database, Hybrid Search]
---

## 摘要
正文略。
"""

PAGE_OTHER_CAT = """\
---
title: 短视频成瘾
category: Media
description: 短视频的多巴胺反馈与成瘾机制
updated: 2026-05-24
sources: 1
entities: [Dopamine, Short-form Video]
---

## 摘要
正文略。
"""

PAGE_BROKEN = """\
这个页面没有 frontmatter，直接是正文。
李四王五。
"""


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str):
        print(f"  {'✓' if cond else '✗'} {msg}")
        if not cond:
            failures.append(msg)

    print("[1] 解析 + catalog 分组")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "AI/rag.md", PAGE_OK)
        _write(root, "Media/short-video.md", PAGE_OTHER_CAT)
        _write(root, "index.md", "# 旧 index（应被忽略，不计入页面）")

        pages = collect_pages(root)
        check(len(pages) == 2, f"index.md 被排除，恰好收集 2 页（实际 {len(pages)}）")

        rag = next(p for p in pages if p.path == "AI/rag.md")
        check(rag.title == "检索增强生成（RAG）", f"title 解析正确：{rag.title!r}")
        check(rag.category == "AI", "category 解析正确")
        check("：" in rag.description and rag.description.endswith("幻觉"),
              f"描述里的冒号原样保留：{rag.description!r}")
        check(rag.entities == ["RAG", "Embedding Model", "Vector Database", "Hybrid Search"],
              f"entities 内联列表解析正确：{rag.entities}")

        cat = build_catalog(root)
        check("## AI" in cat and "## Media" in cat, "catalog 按类别分组（AI 在 Media 前，字母序）")
        check(cat.index("## AI") < cat.index("## Media"), "类别字母序：AI < Media")
        check("结合外部检索" in cat and "Embedding Model" in cat, "catalog 含描述与实体")
        check("最近更新" not in cat, "catalog 不带日期行")

    print("\n[2] regenerate_index 落盘 + 内容一致")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "AI/rag.md", PAGE_OK)
        content = regenerate_index(root, today="2026-05-24")
        on_disk = (root / "index.md").read_text(encoding="utf-8")
        check(content == on_disk, "返回内容与落盘内容一致")
        check("*最近更新：2026-05-24*" in on_disk, "index 带日期行")
        check("[检索增强生成（RAG）](AI/rag.md)" in on_disk, "index 含页面条目（标题+路径）")

    print("\n[3] 冷启动：空 wiki → 合法空 index")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        content = regenerate_index(root, today="2026-05-24")
        check("# Wiki Index" in content and "暂无页面" in content, "空 wiki 产出合法空 index")
        check((root / "index.md").is_file(), "空 wiki 也写出了 index.md 文件")

    print("\n[4] 容错：坏 frontmatter 不崩、仍出现在目录")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "AI/rag.md", PAGE_OK)
        _write(root, "junk/broken.md", PAGE_BROKEN)
        pages = collect_pages(root)
        check(len(pages) == 2, f"坏页不致崩、共 2 页（实际 {len(pages)}）")
        broken = next(p for p in pages if p.path == "junk/broken.md")
        check(broken.title == "broken", f"坏 frontmatter 降级用文件名作 title：{broken.title!r}")
        check(broken.category == "未分类", "坏页归入「未分类」")
        cat = build_catalog(root)
        check("broken.md" in cat, "坏页仍出现在 catalog 里（不丢页）")

    print("\n[5] staging/ 排除出目录与列举（T6）")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "AI/rag.md", PAGE_OK)
        _write(root, "staging/raw-survey.md", PAGE_OK)   # 待归档原料，应被排除

        # collect_pages / build_catalog 不含 staging
        pages = collect_pages(root)
        check([p.path for p in pages] == ["AI/rag.md"],
              f"collect_pages 排除 staging/（实际 {[p.path for p in pages]}）")
        cat = build_catalog(root)
        check("staging/" not in cat, "catalog 不含 staging/ 条目")

        # regenerate 后 index.md 不含 staging
        idx = regenerate_index(root, today="2026-05-29")
        check("staging/" not in idx, "重生 index.md 不含 staging/ 条目")

        # ListFilesTool：默认列举排除 staging/ 与 index.md
        lf = ListFilesTool(root.resolve())
        full = asyncio.run(lf.execute())
        check("staging/" not in full, "list_files() 默认输出不含 staging/")
        check("AI/rag.md" in full, "list_files() 仍列出已策展页面")
        # 显式 list_files('staging') 仍能发现待归档文件（curator 入口不被砍）
        staged = asyncio.run(lf.execute(subdir="staging"))
        check("staging/raw-survey.md" in staged, "list_files('staging') 仍列出待归档文件")

    print()
    if failures:
        print(f"❌ {len(failures)} 项失败")
        return 1
    print("✅ wiki_index 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
