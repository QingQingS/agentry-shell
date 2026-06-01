# 迭代计划 — agentry-shell（iter-01）

> Planner 产出，消费 `reviews/iter-01-eval.md`。日期：2026-05-29。
> 技术栈（已锁定，不更换）：Python3 + asyncio；LLM provider = AsyncOpenAI / Anthropic SDK；
> 后端 = FastAPI + WebSocket；wiki = 文件型 markdown 库。

---

## 1. 本轮目标（一句话）

把项目的**叙事、默认配置、可复现 demo 全部收敛到 v2 hub-and-spoke**，并修掉会让 reviewer
第一眼出戏的脏产物/契约不一致 —— 让面试官能 **60 秒离线跑通、5 分钟读懂难点在哪**。

价值函数对齐：本轮重仓 #1（编排/失败处理可讲成故事）+ #2（README/demo/设计取舍），
robustness 只做"能当谈资且护住 demo"的那一小块，**不做功能广度**。

---

## 2. 处置表（每条 Evaluator 发现 / 存档项 → 本轮做 / 推迟 / won't-fix）

### 第 2 节发现清单

| 发现 | 严重度 | 处置 | 理由 |
|---|---|---|---|
| 复合 happy path 不可靠（arxiv 503 + 无超时） | P0 | **本轮做**（拆成 T1+T2） | 直接卡死 demo；失败处理本身是 #1 谈资 |
| Coordinator 无跨轮记忆，README 却头条宣传连续对话 | P1 | **本轮做（仅文档侧，T4）**；接回 session 记忆 → 推迟 | 叙事一致性是 #2 核心；接回记忆属功能广度，本轮不做 |
| 跨 agent observation 回灌完整报告，与 −72% 自相矛盾 | P1 | **本轮做（T5）** | #1 context 工程的招牌结论，自打脸必须先修 |
| staging 泄漏进 index.md | P1 | **本轮做（T6）** | reviewer 打开 wiki 第一眼的垃圾条目，且是 T9 前置 |
| registry input/output_contract 散文漂移 | P1 | **本轮做（T7，仅手工单一来源对齐）** | 上次大 bug 根因；契约纪律是谈资。codegen 派生推迟 |
| v1 wiki 路由被 staging 打断 + README/.env 默认指 v1 | P1 | **本轮做（T3）** | 与"统一到 v2"是同一件事；默认值必须能跑通 |
| pytest 无法一条命令跑 | P2 | **本轮做（仅文档真实跑法，并入 T4）** | 可复现门面；完整 pytest 套件推迟 |
| 三处 AGENT_CLASS / RETRIEVER 默认打架 | P2 | **本轮做（并入 T3）** | 从零复现摩擦，与默认值统一同批解决 |
| 死代码 + WS 多 spoke token 计量失真 | P2 | **死代码删除本轮做（T8）；WS 计量修复推迟** | 删死代码近零成本；WS 计量只影响 Web UI，非本轮 demo 路径 |
| degenerate 仍兜底落盘"未找到" | P2 | **本轮做（并入 T8）** | 小修，保 wiki 干净，与 T6 同向 |

### 存档分流项（来自第 3 节三栏表）

- 标"已解决"的（research 落盘/staging、BaseLLM tool calling、路由 registry 化、agents/__init__、B4 前端）：
  **不复活，不排任务**。
- 标"已过期/不适用 v2"的（冷启动 intent 降级、v1 wiki"碰巧走通"、orchestrator description 快照）：
  **归 won't-fix**，按 Evaluator 纪律不复活；其中"v1 wiki 实为隐性 bug"已并入 T3 处理。
- 标"仍然有效"但属 v1 dormant / 已披露 in-progress 的
  （session_id 脆弱、v1 同步 IO、POST /api/run 同步、Anthropic 工具路径、intent.files 废纸）：
  **本轮 won't-fix / 推迟**。理由：均不在 v2 活跃 demo 路径上；本轮通过 T3/T4 把 v1 明确标为 dormant
  来收窄影响范围，而不是去修一条要弃用的路径。Anthropic 路径 README 已披露，保持现状。

---

## 3. 本轮任务清单（按执行顺序）

### T1 [P0] 增加离线可复现的 demo 检索模式
- **为什么做**：价值 #2 + Evaluator 杠杆 #2。当前 happy path 受 arxiv 503 拖死，面试官现场跑大概率挂。
- **改动范围**：新增 `RETRIEVER=local`（读 `fixtures/` 下缓存的论文/检索结果），或给现有 retriever 加缓存命中；
  `core/config.py` 检索器选择；新增 1 个 fixtures 数据文件 + 1 条 demo 任务脚本。
- **验收标准**：在**断网**环境下 `RETRIEVER=local python cli.py "<demo 任务>"` 能在 **<60s** 稳定产出
  一份 report 并落盘；不依赖任何外网。
- **依赖**：无。

### T2 [P0] 给 LLM / 检索调用加 per-call 超时 + 503/连接错误重试转 observation
- **为什么做**：价值 #1（失败处理是能讲成故事的点）。无超时使慢/挂调用阻塞整轮（现场 >2.5min 无产出）；
  `core/retrievers/arxiv.py:36` 重试只匹配 "429"，503 首次即 re-raise。
- **改动范围**：LLM provider（`AsyncOpenAI` 调用处）加可配置 timeout（默认 60s）；
  `core/retrievers/arxiv.py` 重试覆盖 503 + 连接错误并带退避；超时/最终失败 → 转成一条 observation 而非抛挂。
- **验收标准**：(a) 注入一个必失败/超时的检索，主 agent 在 ≤2×timeout 内收到 observation 并继续 ReAct，
  不再整轮挂起；(b) 503 触发重试且退避（日志可见）；(c) timeout 值可经 env 配置。
- **依赖**：无（可与 T1 并行）。

### T3 [P1] 统一默认配置到可跑通的 v2 hub + 收口 v1 dormant
- **为什么做**：价值 #2 + 杠杆 #1。三处 `AGENT_CLASS`/`RETRIEVER` 默认互相打架；v1 wiki 路由 staging 后已断。
- **改动范围**：`.env.example`、README Quick start 的 `.env` 块、`core/config.py:42/50` 默认值，
  统一为"开箱即跑通 v2"（`AGENT_CLASS=CoordinatorAgent` + `RETRIEVER=local`）；
  删除或显式隐藏 v1 的 wiki 路由分支（`orchestrator_agent.py:82-85`），并在入口处标注 v1 为 dormant。
- **验收标准**：全新 clone 后照 `.env.example` 直接跑 demo 命令即通（结合 T1）；
  仓库内不再存在"指向 v1 的默认值"；v1 wiki 路由要么删除、要么调用时给出明确"已弃用"提示而非静默读不到。
- **依赖**：T1（默认 demo 需离线可跑）。

### T4 [P1] README 叙事重写：收敛到 v2 + 写出 v1→v2 设计演进
- **为什么做**：价值 #2 + 杠杆 #1。当前 README 头条宣传的连续对话/意图路由是 dormant 的 v1，面试官会读错重点。
- **改动范围**：README —— 架构图换成 v2 hub-and-spoke；新增"设计演进/取舍"小节
  （为何放弃固定意图路由、改 ReAct 涌现式分解）；明确写出 v2 能力边界
  （单任务内分解、**无跨轮记忆**）与 v1 历史能力的区分；补"测试真实跑法"
  （`for f in tests/check_*.py; do python $f; done`）；为 demo 录屏（asciinema/GIF）留占位 + 60s demo 命令。
- **验收标准**：README 不再把任何 dormant v1 能力描述成当前头条；按 README 第一条命令能复现；
  读者能在文中找到"为什么从 v1 变 v2"的明确解释。
- **依赖**：T1、T2、T3（文档须反映改后真实行为）。

### T5 [P1] hub observation 默认只消费 summary/artifact_path，report 全文按需注入
- **为什么做**：价值 #1（context 工程是招牌）。当前 `core/dispatch.py:135-148` 把 spoke 全文报告回灌 hub，
  与 README "−72%"核心结论方向相反，复合任务会累积膨胀。
- **改动范围**：`core/dispatch.py` `_format`：hub observation 默认含 `summary` + `artifact_path`，
  `report` 全文仅在下游确需且无 artifact 时注入或截断；`coordinator_agent.py:199` append 处对应调整。
- **验收标准**：复合（3-spoke）任务下，hub 累计 token **明显低于**改前（给出前后数字对比）；
  hub 仍能据 summary/artifact_path 正确推进后续派发，端到端结果不退化。
- **依赖**：无（建议在 T1 之后用 demo 任务量化对比）。

### T6 [P1] 把 staging/ 排除出策展产物与文件列举
- **为什么做**：价值 #2 + 杠杆 #3 前置。`core/wiki_index.py:80` `rglob("*.md")` 未排除 staging，
  `wiki/index.md` 已含空描述垃圾条目；`ListFilesTool` 同样泄漏。
- **改动范围**：`core/wiki_index.py` `collect_pages` 与 `ListFilesTool` 排除 `staging/`（及 `index.md`）；
  重生 `wiki/index.md`；`tests/check_wiki_index*` 加断言。
- **验收标准**：重生 index 后不含任何 `staging/` 条目；新增断言通过；`list_files` 输出不含 staging。
- **依赖**：无。

### T7 [P1] registry 契约与 Coordinator SCHEMA 单一来源对齐
- **为什么做**：价值 #1（契约纪律是谈资）。`core/registry.py:88` `wiki_curator.input_contract` 仍是 path-based，
  与 `coordinator_agent.py:58-67` 的 stage-first SCHEMA 冲突 —— 正是上次大 bug 的同型根因。
- **改动范围**：`core/registry.py` 的 wiki_curator input/output_contract 改写为与 SCHEMA 一致
  （必须先 `stage_files`、wiki_curator 只读 `staging/`）；两处对同一下游不得再给冲突指引。
- **验收标准**：registry 契约文字与 SCHEMA 描述一致、无矛盾；派发 wiki 归档任务时 LLM 不再被引导去猜文件路径。
  （codegen 从 schema 派生属推迟项，本轮只要求人工单一来源。）
- **依赖**：无。

### T8 [P2] 清理：死代码删除 + degenerate 不落盘
- **为什么做**：价值 #2（干净度）。`core/stream.py:14` `stream_output` 无人调用；
  `websocket_manager.py:60` `active` 队列从不消费；`research_agent.py:179` degenerate 仍兜底落盘"未找到"。
- **改动范围**：删除 `stream_output` 与未消费的 `active` 队列；`research_agent.py` 在 `status=degenerate` 时
  不落盘、不产 artifact，observation 明示无结果。
- **验收标准**：死代码删除后全部 `check_*.py` 仍绿；构造一次 degenerate 检索 → 无新 artifact、observation 含"无结果"。
- **依赖**：无。

### T9 [P1 / 本轮 stretch] 给 Coordinator chat 路径加只读 wiki 检索，闭合 research→curate→reuse
- **为什么做**：价值 #1 + 杠杆 #3 —— multi-agent 知识系统最有说服力的一幕；当前 wiki 只写不读，故事缺后半句。
- **改动范围**：新增只读 `wiki_search` / `read_file` 工具（检索已归档页面），注册进 Coordinator 的 chat 工具集；
  **不做**排序/向量化等复杂检索，能按标题/关键词命中已归档页面并被引用即可。
- **验收标准**：先归档一篇 report，再在 chat 路径提问相关问题，Coordinator 能检索到并引用该已归档页面
  （demo 可录制 research→curate→reuse 三段）。
- **依赖**：T6（先让 index 干净，检索不命中 staging 垃圾）。
- **容量说明**：若 T1–T8 耗尽本轮 Generator 预算，T9 顺延为**下一轮头条**，不强塞导致 scope 蔓延。

---

## 4. 本轮明确不做（防 scope 蔓延）

- **不**把 SessionManager 接回 Coordinator（跨轮记忆）—— 属功能广度；本轮只在 README 标清能力边界（T4）。
- **不**修 WS 多 spoke token 计量失真 —— 只影响 Web UI，不在本轮 CLI demo 路径。
- **不**实现 Anthropic 工具调用路径 —— README 已披露为 text-path，保持现状。
- **不**动 v1 的 session_id 脆弱 / 同步 IO / `POST /api/run` 同步 —— v1 dormant，本轮把它标为 dormant 而非修它。
- **不**做 registry 契约从 code schema codegen 派生 —— 较大重构，本轮只人工单一来源对齐（T7）。
- **不**搭完整 `pytest` 套件 —— 本轮只在 README 给出真实可跑命令（T4）。
- **不**碰任何 out-of-scope：高可用/水平扩展、生产监控、多租户、安全沙箱、合规。

---

## 5. 执行顺序与依赖图

```
T1 ─┬─> T3 ─> T4
T2 ─┘         ^
T5 ───────────┘ (用 T1 的 demo 量化)
T6 ─> T9(stretch)
T7  (独立)
T8  (独立)
```
建议顺序：T1, T2（解锁 demo）→ T6, T7, T8（独立小修，清门面）→ T5（招牌指标）
→ T3 → T4（叙事收口，依赖前面真实行为）→ T9（stretch，闭环）。
