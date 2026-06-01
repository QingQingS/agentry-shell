# iter-01 Generator 改动记录

> 消费 `plans/iter-01-plan.md`。日期：2026-05-29。
> 验证基线：`for f in tests/check_*.py; do python "$f"; done` → 12/12 PASS（每个任务后均跑过）。
> 注：本机无任何 LLM API key（OPENAI/ANTHROPIC/DEEPSEEK 全 unset），故端到端 `cli.py`
> 无法真跑；所有行为验证用既有 FakeLLM 模式（monkeypatch `get_llm`）+ 真实
> 检索器/工具完成，必要处用禁用 socket 证明零外网。验证脚本是一次性的（放 /tmp，未入库）。

---

## 逐任务

### T1 [P0] 离线 RETRIEVER=local + fixtures + demo 脚本 ✅
**动了的文件**：`agents/research_agent.py`（`_make_retrievers` 加 `local` 分支 + 常量 `LOCAL_FIXTURES_DIR`，import `LocalFileRetriever`）；新增 `fixtures/llm-agents.md`（1 个语料文件）；新增 `scripts/demo.sh`（1 条 demo 脚本，可执行）。
**验收满足 & 验证**：
- `RETRIEVER=local` 走 `LocalFileRetriever("fixtures")`，`build_research_registry` 因 source_name=`local_file` 把它喂给所有原子工具与 `do_broad_survey`，全程不触外网。
- 验证：(a) 真实 `LocalFileRetriever` 读 `fixtures/` 命中 3 条；(b) FakeLLM 驱动 ResearchAgent 全程 `RETRIEVER=local`，**禁用 `socket.connect`** 下仍产出 report 并落盘 `reports/llm-agents-demo.md`。
**关键决定（见上报事项 #1、#2）**：「断网」按你裁决取「仅检索层离线」；检索器选择实际在 `research_agent._make_retrievers`（白名单写的是 `core/config.py`），按「最小改动 + 实际选择点」改之。

### T2 [P0] LLM/检索超时 + 503/连接错误重试转 observation ✅
**动了的文件**：`core/llm/openai_provider.py`（OpenAIProvider 加 `timeout`，默认 60s、`LLM_TIMEOUT` 可覆盖、显式 kwarg 优先，传给 `AsyncOpenAI`）；`core/retrievers/arxiv.py`（`_is_retriable` 覆盖 503/502/500/连接/超时；重试带退避 + `logging.warning` 可见日志）。
**验收满足 & 验证**：
- (a) 注入必失败检索（retriever.search 抛错）→ 经既有 `ToolRegistry.execute` 边界转成 `Error:` observation，循环继续到下一步写 final；5s 内完成、不挂整轮（status=degenerate）。
- (b) 注入 503 → 重试 3 次、退避 (10,20,40)、3 条可见 `warning` 日志；非瞬时错误（ValueError）立即抛、不退避。
- (c) `LLM_TIMEOUT` env 可配：default=60 / env=5 / kwarg=12 均验证。
**范围说明**：白名单点名「AsyncOpenAI 调用处」，故 timeout 只加在 OpenAIProvider（含 DeepSeek 子类）；Anthropic provider 未加（见 Observations）。

### T3 [P1] 统一默认配置到 v2 hub + v1 dormant ✅
**动了的文件**：`core/config.py`（`agent_class` 默认→`agents.coordinator_agent.CoordinatorAgent`；`retriever` 默认→`local`）；`.env.example`（同上 + 注释）；`README.md` 的 Quick-start `.env` 块（原 `AGENT_CLASS=...OrchestratorAgent` / `RETRIEVER=arxiv` → Coordinator / local）；`agents/orchestrator_agent.py`（docstring 加 DORMANT 横幅；wiki 路由分支前加明确「已弃用」日志）。
**验收满足 & 验证**：
- 隔离环境（无 .env、清空 env）`Config.from_env()` → agent_class=CoordinatorAgent、retriever=local，且 CoordinatorAgent 可 import。
- 仓库内已无「指向 v1 的默认值」（`.env` 本身被 `.gitignore`，未追踪；grep 三处确认无 OrchestratorAgent/EchoAgent 作默认）。
- v1 wiki 路由不再静默：调用时 yield 明确「[已弃用]…请改用 v2 CoordinatorAgent」日志。
**关键决定（见上报事项 #3）**：wiki 路由选择「保留派发 + 加明确弃用日志」而非删除/早退，以满足「给出明确提示而非静默」又**不破坏** `check_orchestrator.py`（白名单外，断言 wiki 路由仍转发 FakeWikiAgent 结果）。

### T4 [P1] README 叙事重写收敛 v2 ✅
**动了的文件**：`README.md`（整体重写）。
**验收满足 & 验证**：
- 架构图换成 v2 hub-and-spoke（Coordinator → dispatch_agent → researcher/wiki_curator，强调只传 summary+artifact）。
- 新增「Design evolution: v1 → v2」小节，解释为何弃固定意图路由、改 ReAct 涌现式分解。
- 明确能力边界：**v2 仅单任务内分解、无跨轮记忆**；跨轮 session 记忆是 v1（dormant）能力、属未来工作。
- 「Running the checks」给出真实跑法 `for f in tests/check_*.py; do python "$f"; done`。
- 新增「60-second offline demo」（`bash scripts/demo.sh` + RETRIEVER=local 说明）+ asciinema/GIF 录屏占位（TODO）。
- 验证：grep 确认无残留 v1 头条（"remembers the conversation"/"Intent-driven orchestration"/"continuous-conversation" 已移除）；README 引用的全部路径存在；记录的测试命令真跑 12/12 PASS。

### T5 [P1] hub observation 默认只消费 summary/artifact_path ✅
**动了的文件**：`core/dispatch.py`（`_format`：有 artifact 且 report 超 `_REPORT_PREVIEW=600` 时截断为预览 + 指向 artifact；无 artifact 时原样带回；更新过时的 2026-05-28 docstring）。
**验收满足 & 验证**：
- 3-spoke 复合任务（每份 ~1569 字报告 + artifact）：hub 注入 observation 合计 4962→2175 字（**−56.2%**）；报告越长降幅越大。
- hub 仍可见 summary + artifact 行，可继续 `stage_files`→`wiki_curator`；无 artifact 的短报告（degenerate 等）不截断；端到端不退化。
**关键决定（见上报事项 #4）**：spec 原文「report 全文仅在…无 artifact 时注入**或截断**」→ 取「有 artifact 即截断预览」，既满足 −token 目标，又因阈值(600) > 测试 SAMPLE_FINAL(74) 而**不破坏** `check_research_spoke.py`（白名单外，多处断言 observation 含 report 全文）。`coordinator_agent.py:199` 消费的是已截断 obs，截断集中在 `_format`，故该行无需改代码。

### T6 [P1] staging/ 排除出策展产物与文件列举 ✅
**动了的文件**：`core/wiki_index.py`（`collect_pages` 排除 `staging/`；加 `STAGING_DIRNAME`）；`core/tools.py`（`ListFilesTool` 默认列举排除 `staging/`，但 `list_files('staging')` 显式列举不过滤）；重生 `wiki/index.md`（去掉 staging 垃圾条目）；`tests/check_wiki_index.py`（新增 [5] 段断言）。
**验收满足 & 验证**：
- 重生后 `wiki/index.md` 不含任何 staging 条目（4 个已策展页）。
- 新增断言：collect_pages/catalog/index 均排除 staging；`list_files()` 不含 staging、仍列已策展页；`list_files('staging')` 仍能发现待归档文件（curator 入口未被砍）。全绿。
**关键决定（见上报事项 #5）**：白名单括注「（及 index.md）」未照做——验收只要求「list_files 不含 staging」，且排除 index.md 会破坏 `check_tools.py`（白名单外，断言 list_files 含 index.md）。按「不多不少 + 不碰白名单外 + 最小改动」只排除 staging。

### T7 [P1] registry 契约与 Coordinator SCHEMA 单一来源对齐 ✅
**动了的文件**：`core/registry.py`（`wiki_curator.input_contract` 由 path-based「把 reports/foo.md 归档」改写为 stage-first：必须先 `stage_files`、wiki_curator 只读 staging/、引用 `staging/foo.md`；output_contract 补「index 由系统重生」）。
**验收满足 & 验证**：catalog 文本与 `SCHEMA_TEMPLATE` 一致（均 stage-first / 只读 staging/）；旧 path-guess 引导（「含要处理的 .md 文件路径」「把 reports/foo.md 归档」）已移除，并明确「不要把 reports//uploads/ 直接交给它」。无测试绑定旧文字，全绿。

### T8 [P2] 死代码删除 + degenerate 不落盘 ✅
**动了的文件**：删除 `core/stream.py`（`stream_output` 仅被 `core/__init__.py` 导出、无真实调用方）；`core/__init__.py`（移除该 import 与 `__all__` 项）；`backend/server/websocket_manager.py`（`self.active` 从 `Dict[WebSocket, asyncio.Queue]`（队列值从不消费）改为 `Set[WebSocket]`，去掉无用 import asyncio）；`agents/research_agent.py`（degenerate 判定提前到兜底落盘前，degenerate 时不调 `_fallback_save`、不产 artifact）。
**验收满足 & 验证**：
- `import core` 正常、`__all__` 不再含 stream_output；WebSocketManager 可 import；连接计数仍用 `len(self.active)`。
- degenerate 检索（空 retriever）：`reports/` 0 文件、observation 含 `status=degenerate` + 「（未检索到相关结果）」、无 `artifact:` 行。
- 全部 check_*.py 仍绿。
**注**：保留 degenerate 仍带 `report:` 段与 summary 文字「（未检索到相关结果）」，因 `check_research_spoke.py:206/208` 绑定之（白名单外）；「observation 含无结果」按「明示无检索结果」理解满足（见上报事项 #6）。

### T9 [P1/stretch] Coordinator chat 只读 wiki 检索 ✅
**动了的文件**：`core/tools.py`（新增只读 `WikiSearchTool`：关键词重叠检索已策展页面，排除 staging/index——依赖 T6；import `re`/`collect_pages`）；`agents/coordinator_agent.py`（注册 `WikiSearchTool` + 复用 `ReadFileTool` 进 Coordinator 工具集，rooted at `wiki/`；SCHEMA 增 wiki_search/read_file 说明；import Path）。
**验收满足 & 验证**：
- 单元：wiki_search 命中已策展页、排除 staging；read_file 读全文；无关词返回「未检索到」。
- 集成：FakeLLM 驱动 Coordinator chat 路径 `wiki_search → read_file → 最终答复引用 AI/rag.md`；specs 含 wiki_search/read_file；wiki_search observation 命中归档页。
- 全套回归绿。
**范围说明**：按 spec 不做排序/向量化，朴素词重叠即可（见 Observations）。

---

## Observations / 留给 Planner

1. **Anthropic provider 无 per-call timeout**：T2 白名单点名「AsyncOpenAI 调用处」，故只给 OpenAIProvider（含 DeepSeek）加了 timeout。`core/llm/anthropic_provider.py` 仍无超时，若切到 anthropic provider 仍可能挂；建议下轮补齐（一致性）。
2. **真·无 key 离线 demo 需要假/本地 LLM provider**：当前「离线」仅检索层（已落实）；要做到「无任何 API key 也能跑通端到端 demo」，需新增一个离线/fake LLM provider（读 fixtures 拼报告或本地模型）。属功能新增，超本轮 T1 白名单，留给你定夺。
3. **本机 `.env`（未追踪）注释陈旧**：`.env`（被 .gitignore）里 `# RETRIEVER=arxiv  # 默认` 注释已与新默认（local）不符；它非仓库文件、不在白名单，未改。提一句以免误导。
4. **registry 契约仍手工维护**：T7 只做了人工单一来源对齐；从 Coordinator SCHEMA / 工具 schema codegen 派生契约仍是更稳的根治（plan 已列为推迟项）。
5. **WikiSearchTool 为朴素词重叠**：无排序/向量化（按 spec）。reuse 召回质量有限（中文分词靠 `\w+`，长查询可能误命中高频词）。若 T9 要成「招牌一幕」，下轮可考虑 BM25/embedding。
6. **WS 多 spoke token 计量失真**：本轮按 plan 未碰（只影响 Web UI），仍在；T8 把 `active` 改 Set 不影响该问题。
7. **无 committed 的离线集成测试覆盖 T1/T2/T5/T9 的新路径**：我用一次性 /tmp 脚本 + FakeLLM 验证了，但未入库（这些任务白名单不含新增测试文件，仅 T6 在 check_wiki_index 内加了断言）。若希望长期回归保护，建议下轮排「为 RETRIEVER=local / dispatch 截断 / wiki_search 各补 check_*.py」。

## 上报事项（需要你裁决/已用裁决推进的）

1. **[已用你的裁决推进] T1「断网/不依赖任何外网」歧义**：与改动范围（仅检索层、无 LLM）矛盾，且本机无 key。已问询，你选「仅指检索层离线」，据此实现并验证。若后续想要真·无 key 离线，见 Observations #2。
2. **[已按最小改动推进] T1 检索器选择点位置**：白名单写「core/config.py 检索器选择」，但实际选择逻辑在 `agents/research_agent._make_retrievers`。我改了后者（真正选择点），未改 config.py 的解析逻辑（config 默认值在 T3 改）。如认为越界，请指正。
3. **[已按不破坏白名单外测试推进] T3 v1 wiki 路由处置**：plan 要「删除或显式提示已弃用」。删除/早退会破坏白名单外的 `check_orchestrator.py`（断言 wiki 路由仍派发并转发结果）。我选「保留派发 + 加明确弃用日志」满足「非静默」。若你更想要「彻底删除该路由」，需授权一并改 `tests/check_orchestrator.py`。
4. **[已按 spec 字面 + 不破坏白名单外测试推进] T5 截断 vs 删除**：spec「无 artifact 时注入**或截断**」→ 我取「有 artifact 即截断预览(600 字)」。若你本意是「有 artifact 就完全不带 report」，需授权改 `tests/check_research_spoke.py`（4 处断言 observation 含 report 全文，白名单外）。
5. **[已按验收最小集推进] T6 是否排除 index.md**：白名单括注「（及 index.md）」，但验收只要求「list_files 不含 staging」，且排除 index.md 破坏白名单外 `check_tools.py`。我只排除 staging。若确需 list_files 也排除 index.md，需授权改 check_tools.py。
6. **[按语义理解满足] T8「observation 含无结果」**：未改 degenerate 的 summary 文字（"（未检索到相关结果）"，被白名单外 check_research_spoke 绑定），按「明示无检索结果」理解满足。如需 observation 出现字面「无结果」三字，需授权改测试文字。
