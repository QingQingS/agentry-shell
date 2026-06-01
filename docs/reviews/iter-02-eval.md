# 项目评估报告 — agentry-shell（iter-02 / 进展验证轮）

> 评估者：独立 Evaluator（未参与开发）。日期：2026-05-29。
> 方法：以「实际运行 + file:line 证据」为准，不默认信任 `changes/iter-01-changes.md` 里的 ✅。
> 本轮读入：`plans/iter-01-plan.md`（验收标准）、`changes/iter-01-changes.md`（待验证声称）、
> `reviews/iter-01-eval.md`（上轮基线，用于已修/未修/新增跟踪）。更早历史文件按纪律不重读。
> 写入新文件 `iter-02-eval.md` 而非覆盖 `iter-01-eval.md`，以保留进展基线。
>
> **运行基线（本机无任何 LLM key、无 pytest）**：
> - `for f in tests/check_*.py; do python "$f"; done` → **12/12 PASS**。
> - 亲自跑通的失败/旁路行为（绕开 LLM）：
>   T1 `LocalFileRetriever`（`socket.connect` 全禁下命中 5 条，`source=local_file`）；
>   T2 `_is_retriable`（503/429/URLError/TimeoutError=True、ValueError=False，退避 (10,20,40)）；
>   T5 `DispatchAgentTool._format`（有 artifact→截断 707 字含「预览已截断」；无 artifact→2000 字全文原样）；
>   T6 `collect_pages(wiki)`→4 页、staging 0 条；T9 `WikiSearchTool` 命中 `AI/agent-evaluation.md`、无 staging 泄漏、空查询兜底。
> - **未能亲验（待核实）**：T1/T4 端到端 demo（`scripts/demo.sh` 仍需 LLM key，本机无 key → `<60s 出 report 并落盘` 无法实测）；Web UI 路径（无运行）。

---

## 1. 一句话总体结论

**iter-01 计划的 9 个任务（T1–T9）全部落地，且逐条经当前代码 + 可离线复现的行为核验通过——
项目已从上轮「单元闭环扎实但端到端鲁棒性/文档一致性未到 review-grade」推进到「v2 demo 路径达 review-grade」：
叙事收敛到 v2、失败处理真实可讲、−72% 的 context 工程主张现在有代码兜底、wiki 写入→复用闭环成型。
唯一未闭合的最后一厘米是「让一个没有 LLM key 的面试官也能 5 分钟看懂它在跑」——demo 录屏仍是占位，
keyless 复现仍无入口。**

---

## 2. 本轮发现清单（按严重度）

> 说明：iter-01 的 P0/P1/P2 经本轮验证基本已修（见第 3 节进展表），故本节只列**本轮新发现 / 仍残留**项，
> 严重度按「对作品集故事的影响」校准，不据 out-of-scope 扣分。

### [P1] 「60 秒离线 demo」对无 LLM key 的 reviewer 仍不可跑，且录屏仍是占位
- **证据**：`scripts/demo.sh:5` 与 `README.md:192` 均明示「仅需一个 LLM API key（agent 推理循环要调模型）」——
  这点 README **诚实未夸大**（offline 仅指检索层，line 196/281 措辞准确）。但 `README.md:199-200`
  仍是 `📹 Demo recording placeholder … (TODO: record scripts/demo.sh and embed)`。
  合起来的后果：检索层离线已坐实（本轮 socket 全禁下实测 `LocalFileRetriever` 命中 5 条），
  但**端到端 demo 的可观测产物对 keyless reviewer 是空的**——既跑不了、也没录屏可看。
  本评估者正因无 key 而无法实测 T1 验收的 `<60s 出 report 并落盘`。
- **验收标准**：录制并嵌入 `scripts/demo.sh` 的 asciinema/GIF（替换 line 199-200 占位），
  使无 key 的 reviewer 能在 README 内直接看到一次完整 research→落盘 的运行；
  录屏旁注明「检索离线、推理需 key、本次实测耗时 Xs」。（可选 stretch：提供一个 fake/本地 LLM
  provider 让端到端真·无 key——changes Observation #2 已提，属功能新增，本轮不强求。）

### [P2] T5 截断后仍把 600 字 report 预览注入 hub，与「只消费 summary/artifact」有口径差
- **证据**：`core/dispatch.py:153-159`：有 artifact 时 report 截到 `_REPORT_PREVIEW=600` 仍随 observation 带回 hub。
  plan T5 的措辞是「hub 默认**只消费** summary/artifact_path」。实测 3-spoke 下每 spoke 仍注入 ~600 字预览
  （3×600≈1.8k 字），降幅 −56%（changes 自报）真实但非「移出循环」。这是**可辩护的设计取舍**
  （给 hub 一眼可读的钩子），不是 bug；只是讲「−72%／把全文移出循环」时口径要对齐，否则面试官会追问。
- **验收标准**：二选一并在 README/代码注释里说清——(a) 把阈值降到「仅 summary+artifact，0 预览」并量化新降幅；
  或 (b) 保留 600 预览但在叙事里改称「summary + 短预览 + artifact 指针」，不再宣称「全文移出循环」。

### [P2] 死代码清理留下的文档漂移：WebSocketManager docstring 仍描述已删除的队列
- **证据**：T8 已把 `self.active` 从 per-connection `asyncio.Queue` 改为 `Set[WebSocket]`
  （`backend/server/websocket_manager.py:54` 实测为 Set），但同文件 `:47-48` docstring 仍写
  「每个连接独立维护一个消息队列 / Agent 生成的 AgentEvent 通过队列异步推送」。代码与注释自相矛盾。
- **验收标准**：更新 docstring 为「`active` 仅 Set 计数，事件经 `_send_event` 直推」；与代码单一来源。

### [P2] 仓库根目录有未追踪噪声（reviewer 第一眼可见），且 iter-01 全部改动仍未提交
- **证据**：`git status` 显示 `trace.log`、`plan.md`（旧根级）、`evaluator.md` 等未追踪；T1–T9 的 16 个修改 + 1 删除
  全在工作区**未提交**。对「从零按 README 复现」与「commit 历史能讲演进故事」都是减分项。
- **验收标准**：`trace.log` 入 `.gitignore`、旧 `plan.md` 删除或归档；iter-01 成果按任务粒度小步提交
  （`git log` 能看出 v2 收敛的演进轨迹）。

---

## 3. iter-01 发现 → 本轮验证进展表（已修 / 仍残留 / 新增）

| iter-01 项（出处） | 上轮严重度 | 本轮判决 | 证据（亲验） |
|---|---|---|---|
| 复合 happy path 受 arxiv 503 + 无超时拖死 | P0 | **已修** | `arxiv.py:21-27` 503/502/500/conn/timeout 均 retriable（实测）；退避 (10,20,40)；`openai_provider.py:89-98` per-call timeout 默认 60s、`LLM_TIMEOUT` 可覆盖、传入 `AsyncOpenAI`。失败转 observation 由 `ToolRegistry.execute` 边界保证 |
| Coordinator 无跨轮记忆但 README 头条宣传连续对话 | P1 | **已修（文档侧）** | `README.md:50/108-109` 已把 session memory 标为 v1 dormant + future work；无残留 v1 头条（grep 仅剩对照性描述）。接回记忆按 plan 推迟，非本轮 |
| 跨 agent observation 回灌全文报告，与 −72% 矛盾 | P1 | **已修（见 §2 P2 口径差）** | `dispatch.py:153-159` 有 artifact 即截断 600 预览；实测有/无 artifact 行为分叉正确，−56% |
| staging 泄漏进 index.md | P1 | **已修** | `wiki_index.py:79-100` collect_pages 排除 staging；`tools.py:127-143` list_files 默认排除、显式 `list_files('staging')` 仍可见；实测 index 4 页 0 staging |
| registry input/output_contract 散文漂移 | P1 | **已修** | `registry.py:88-94` wiki_curator 契约改为 stage-first、引用 `staging/`，与 `coordinator_agent` SCHEMA 一致 |
| v1 wiki 路由被 staging 打断 + 默认指 v1 | P1 | **已修/已缓解** | `config.py:42-55` 默认 Coordinator+local（隔离 from_env 实测）；`orchestrator_agent.py:86-93` v1 wiki 路由保留入口但 yield 明确「[已弃用]」日志，不再静默 |
| pytest 无法一条命令跑 | P2 | **已修（文档侧）** | README 给出真实跑法 `for f in tests/check_*.py; do python "$f"; done`，实测 12/12 |
| 三处 AGENT_CLASS/RETRIEVER 默认打架 | P2 | **已修** | `.env.example` 与 `config.py` 默认统一 Coordinator+local；grep 无残留 v1 默认 |
| 死代码 stream.py / WS active 队列无用 | P2 | **已修** | `core/stream.py` 已删、`core/__init__` 无引用；`websocket_manager.py:54` 改 Set（**但 docstring 漂移，见 §2**） |
| degenerate 仍兜底落盘「未找到」 | P2 | **已修** | `research_agent.py:180-191` is_degenerate 提前判定，degenerate 时跳过 `_fallback_save`、不产 artifact |
| Coordinator chat 只写不读 wiki（杠杆#3） | — | **已闭环** | `tools.py:200-250` 新增只读 `WikiSearchTool`；`coordinator_agent` 注册 wiki_search+read_file；实测命中归档页、无 staging 泄漏 |
| WS 多 spoke token 计量失真 | P2 | **仍残留（按 plan 推迟）** | 只影响 Web UI，非 CLI demo 路径；T8 改 Set 不触及 |
| Anthropic 工具/超时路径 | P1(v1) | **仍残留（按 plan 推迟）** | `openai_provider` 加了 timeout，`anthropic_provider` 未加；README 已披露为 text-path |
| v1 session_id 脆弱 / 同步 IO / POST /api/run 同步 / intent.files 废纸 | P1-P2(v1) | **仍残留（won't-fix 本轮）** | 均 v1 dormant 路径，plan 明确不修，已用 dormant 横幅收窄影响 |

> 无回归（regression）发现：本轮验证未见任一已修项因改动而重新破裂；12/12 check 全绿。

---

## 4. 针对「作品集故事」的 3 条最高杠杆改进

1. **补上 demo 录屏，闭合「keyless reviewer 也能看懂在跑」的最后一厘米。** 这是当前唯一 P1。
   检索离线已坐实、叙事已收敛，但 `README.md:199` 还是占位 GIF——面试官没 key 时既跑不了也看不到。
   录一段 `scripts/demo.sh` 的 asciinema 嵌进 README（注明耗时与「推理需 key/检索离线」），
   一步把杠杆 #2 从「半成品」变「成品」。若想更狠，加一个 fake LLM provider 做到端到端真·无 key。

2. **把「−72% / context 工程」的口径与代码对齐，主动讲透取舍。** 现在 dispatch 仍注入 600 字预览（−56%，非移出全文）。
   与其让面试官追问「那到底移没移出去」，不如在 README 设计取舍小节直接写：
   「hub 只吃 summary + artifact 指针 + 短预览；为什么留预览（hub 决策需一眼可读）、为什么不留全文（复合任务累积膨胀）」——
   把一个被追问点变成展示工程判断的得分点。

3. **用 commit 历史和干净的仓库根把「v1→v2 演进」讲成可看的故事。** iter-01 的 16 处改动现在全堆在工作区未提交，
   `trace.log`/旧 `plan.md` 散在根目录。按任务粒度小步提交（T1…T9 各一 commit）+ gitignore 噪声，
   让 `git log` 本身成为「我如何把项目从 v1 意图路由收敛到 v2 hub-and-spoke」的叙事载体——
   这对展示工程节奏比任何 README 段落都有说服力。
