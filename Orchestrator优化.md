# OrchestratorAgent 重构讨论

> 目标：把当前「意图分类 → 单 worker 路由」的 Orchestrator，重构为
> **hub-and-spoke 多 agent 动态分解架构**。本文档为讨论存档，供续接。
>
> 起始日期：2026-05-26

---

## 一、当前 OrchestratorAgent 的问题（重构动机）

### Tier 1 — 正确性 bug
1. **`session_id = id(self.websocket)` 脆弱**（orchestrator_agent.py:46-47）
   - id 再利用：WS 被 GC 后新连接可能拿到同一 `id()` → 串入别人 session（实 bug）。
   - `id(None)` 进程内恒定 → 所有 CLI 收敛成一个 session，CLI 无法持有多个独立会话。
   - 无 eviction：`_sessions` dict 是 class-level 单例，进程生命周期内只增不减；`reports/` 磁盘也无限增长。内存泄漏。
2. **同步文件 IO 阻塞 event loop**：`save_report` 里 `file_path.write_text()`（session.py:102）在 async 路径上是 blocking IO，多 WS 并发时会卡住事件循环。

### Tier 2 — 设计/扩展性（与简历「pluggable agent runtime」主张冲突）
3. **路由 if/elif 硬编码**（orchestrator_agent.py:79-88）：直接 import 全部 worker，加一个 route 要改 orchestrator + intent + IntentResult 三处。code review 最易被戳。
4. **class-level 单例 SessionManager**（orchestrator_agent.py:42）：全局可变状态，无法注入、难测、并发难推理。

### Tier 3 — 行为机微
5. **降级默认昂贵**：classify 失败 `_degraded` 返回 research/survey（intent.py:43-44），本是 chat 追问也会跑整套检索。
6. **carry_context 只带最新 1 份**：`_recent_report_text` 取 `ctx.reports[-1]`，相关报告非最新就注入错背景。
7. **worker 失败 → turn 不记录**：`_write_back` 在 async-for 完走后才执行，研究挂了会话历史留洞。
8. **research 专用字段漏进通用 IntentResult**：wiki 下 `mode`/`target` 无意义。

> headline：#1（session_id）+ #3（硬编码路由）。新架构正好一并解掉 #3/#4。

---

## 二、目标架构：Hub-and-Spoke 动态分解

用户愿景（要具备的能力）：
1. **Dynamic Adaptive Decomposition**：主 agent 临场把任务拆成子任务。
2. **Hub-and-Spoke（星型拓扑）+ Context Isolation**：Coordinator 为每个子 agent 创建**孤立上下文**（仅 `System Prompt + input.context + input.prompt`），子 agent 各跑自己的 agentic loop。
3. Coordinator 通过 `Task` 工具派发（示例）：
```json
{
  "role": "assistant",
  "content": [
    {"type": "tool_use", "id": "call_task_market_001", "name": "Task",
     "input": {"agent": "market_researcher", "prompt": "Research AI infra market size",
               "context": "Focus: market size USD, YoY growth, top 3 vendors"}},
    {"type": "tool_use", "id": "call_task_tech_002", "name": "Task",
     "input": {"agent": "tech_analyst", "prompt": "Analyze repo dependencies",
               "context": "Focus: Triton and PyTorch versions"}}
  ]
}
```
4. 并行 vs 串行：无数据依赖→并行（单次 response 多个 tool_use）；有依赖→串行（分步 tool use）。

---

## 三、核心重构观点：依赖靠循环「涌现」，不做显式 DAG

**不要把依赖做成独立的判断步骤 / `depends_on` 的 DAG 规划器。** 理由（用户在 WikiAgent 已拍板）：固定流水线会把控制权从 LLM 手里夺走，agentic 就死了；预先规划完整 DAG 就是另一种固定流水线。

更干净的模型：**Coordinator 本身就是一个 ReAct 循环，它的「工具」就是子 agent（`Task` / `dispatch_agent` 工具）**。并行/串行不是调度器决策，而是循环的自然形状：
- **无依赖** → 一个 assistant turn 吐多个 `tool_use` → orchestrator `asyncio.gather` 并行驱动。
- **有依赖** → 这一轮只发 A，因为 Coordinator 字面上需要 A 的结果文本才能写 B 的 prompt/context；拿到 A 后下一轮再发 B。

依赖被编码在「Coordinator 何时发出某个 Task」里 = 真正的 Dynamic Adaptive（按返回临场调整，而非开局猜图）。

**复用红利**：这套循环 WikiAgent 已写过（MAX_STEPS、错误转 observation、reasoning_content 回传、yield log）。Coordinator 结构上是同一个循环，工具表里只有一个特殊工具 `dispatch_agent`。

---

## 四、承重件（草图欠定义、但决定健壮性）

1. **上游结果如何流到下游子 agent（隔离的另一半）**：B 依赖 A 时，A 的结果由 **Coordinator 蒸馏**后塞进 B 的 `context`。隔离是**双向**的——B 看不到 A 的完整 transcript，只看 Coordinator 转述的精华；兄弟 agent 间零可见。
2. **子 agent 返回契约（呼应 WikiAgent −72% 教训）**：子 agent 绝不把完整 transcript 灌回 Hub（否则 Coordinator 上下文爆炸）。返回**短结构化摘要**，完整产物落盘（复用现成 `save_report` + 窗口机制），Coordinator 只拎摘要。
3. **agent 注册表**：`name → (system_prompt, loop/class)`，是 pluggable 落点 + `Task.agent` 字段的查表对象。一并解掉问题 #3。
4. **错误转 observation**：spoke 挂了不整体崩，把错误作为该 Task 的 result 喂回 Coordinator，让它临场决策（重试/换路/报告部分）。承接 tools.py「永不向循环 raise」。
5. **终止预算**：Coordinator 的 MAX_ROUNDS + 总 spoke 数上限，防递归式无限分解。
6. **事件 fan-in + 归属**：多个并行 spoke 各吐 async 事件流，要合并成一条（`asyncio.Queue` 扇入），每事件带 `spoke_id` 标签，CLI 才能画出「哪个子 agent 在说话」。并行的主要实现复杂度。

---

## 五、三个承重决策（已拍板 2026-05-26）

- **A — 依赖靠涌现，无显式 DAG**：主 agent 自己临场分解。✅
- **B — 子 agent 和主 agent 的最终结果都输出 JSON 结构化**。
  注意：是「最终结果」输出 JSON；主 agent 中间派发轮仍是 `tool_use`，不是 JSON。
- **C — 路由收编进分解**：单 Task = 退化路由，先跑起来看效果，暂不做便宜 fast-path。

---

## 六、返回契约草案（B 的落点，待最终确认）

**子 agent 最终结果：**
```json
{
  "status": "ok | error",
  "summary": "一句话结论，Coordinator 只拎这个进上下文",
  "artifact_path": "reports/xxx.md | null",
  "key_facts": "可选，给下游 spoke / 最终合成用的关键事实"
}
```

**主 agent 最终结果：**
```json
{
  "answer": "markdown 正文 —— 真正呈现给用户的，不是裸 JSON",
  "spokes_used": ["market_researcher", "tech_analyst"]
}
```
关键：`answer` 是 markdown 渲染给用户，**用户不该看到裸 JSON**。解析沿用 intent.py 老办法（正则抠 `{...}` + 失败降级）。

---

## 七、待决 fork：现有 3 个 agent 怎么变成 spoke？

现状：ResearchAgent / ChatAgent / WikiAgent 都 yield `AgentEvent` 流、以一个 `result` 事件（自由 markdown）收尾。要满足 B：

- **(1) 外壳包装**：agent 不动，orchestrator 把 spoke 的 `result` 文本塞进 JSON。代价：`summary` 还要 orchestrator 再调一次 LLM 蒸馏（额外成本），且非「子 agent 自己输出」，违背 B 精神。
- **(2) 各 agent 自己产出结构化 result**（**Claude 倾向**）：每个 agent 改成最终 emit 结构化结果（自带 summary）。更忠于 B、agent 自治、hub 保持精简。WikiAgent 几乎现成（有 `touched_files`），ResearchAgent 末尾加一轮自摘要。代价：3 个 agent 各小改。

> **下次从这里继续**：用户对 fork (1)/(2) 的选择。
> 若选 (2)，拆步骤设想（每步独立可验证闭环）：
> 1. `Task`/`dispatch_agent` 工具 + agent 注册表（复用 core/tools.py 风格）。
> 2. Coordinator ReAct 循环，单 spoke 跑通退化路由（= 替代现路由器）。
> 3. 多 spoke 并行（`asyncio.gather` + 事件扇入 + spoke_id 归属）。
> 4. 串行依赖（Coordinator 蒸馏上游结果注入下游 context）。
>
> 另需在重构中顺手修 #1（session_id）、#2（async 文件 IO），它们与新架构正交但必须解。

---

## 八、2026-05-27 敲定：fork (2) + 入参统一 + 智能下沉

### 8.1 结构化结果走 `metadata`，不塞进 `content`（fork (2) 的干净实现）
选 **fork (2)（各 agent 自产结构化结果）**，但**落点是 `AgentEvent.metadata` 而非 `content`**：
```python
yield AgentEvent(type="result", content=markdown,
                 metadata={"status","summary","artifact_path","key_facts"})
```
- `content` 永远是人类 markdown → agent **独立运行**（`AGENT_CLASS=agents.x`）时 CLI 直接渲染，不会给用户看裸 JSON（决策 B 的「无裸 JSON」天然满足）。
- `metadata` 带结构 → Coordinator 只读 metadata 拎 summary，**spoke 结果无需正则抠 JSON**（intent.py 那套降级解析只在 Coordinator 自己的最终 answer 上才可能用到）。
- 仍是 agent 自产（忠于 fork 2），但避开 JSON-as-content 破坏独立 UX 的副作用。

### 8.2 ChatAgent 被吸收进 Coordinator（不是删类，是架构后果）
Coordinator 本身是带对话上下文的 LLM 循环，手里已握 session 报告摘要 + 最近几轮。「chat 追问」= **它不派任何 spoke，直接作答**。路由退化成连续谱：

| 发出 Task 数 | 等价旧世界 | 行为 |
|---|---|---|
| **0** | ChatAgent | Coordinator 直接用上下文作答（无检索） |
| **1** | 旧单 worker 路由 | 退化路由（决策 C） |
| **N** | —（新能力） | 动态分解 |

ChatAgent 类可删，其「凭已有上下文作答」的本事迁进 Coordinator system prompt。「0 Task = chat」是「1 Task = 退化路由」的下沿，与决策 A/C 一致。

### 8.3 入参统一为 `(prompt, context)`，智能下沉进 spoke ← 推翻「输入不对称」担忧
之前担心「ResearchAgent 吃查询串、WikiAgent 吃文件列表」的不对称——**那是抱着 intent.py 预解析字段（target/mode/files）不放造成的假问题**。一旦承认预解析本就要废，不对称消失：

**两个 spoke 入参是同一形状 `(prompt, context)`，agent 自己读 prompt 决定怎么做。**
- ResearchAgent：自己 triage（广度调研 / 找论文 / 读代码库），`mode` 不再外部钉死。
- WikiAgent：收「把 reports/foo.md 归档」这类指令，自己抠 `.md` 路径（`_resolve_input_paths` 已有雏形）。

推倒的东西：

| 旧（hub 预解析） | 新（spoke 自决） |
|---|---|
| intent.py 一次 fast LLM 钉死 route/mode/target/files | **intent.py 退役**；Coordinator 只决定派给谁 + 子任务是什么 |
| `ResearchMode` 是 intent↔agent 跨层契约 | `ResearchMode` 降级为 **ResearchAgent 内部实现细节** |
| dispatch 要按 agent 设不同字段（假不对称） | dispatch 统一 `Task(agent, prompt, context)` |
| WikiAgent 靠注入 `files=[...]` | WikiAgent 从 prompt 自己抠路径 |

**决策分层更干净**：Coordinator 管 WHICH agent + WHAT 子任务（路由+分解）；spoke 管 HOW 执行（自定 mode/检索器/拆子问题）。

**唯一真实代价**：ResearchAgent 入口加一个自我 triage（prompt→mode）。但这步今天就由 intent.py 在 hub 做——只是从 hub **搬进 spoke**，token 量级相当，换来封装干净 + 能力释放。

### 8.4 dispatch 工具彻底定型
```
Task(agent: str, prompt: str, context: str)
```
入参统一（自然语言），出参统一（`{status, summary, artifact_path, key_facts}`，三态 ok / ok-but-degenerate / error）。注册表每项携带 `name / description / input_contract / output_contract`，渲染进 Coordinator system prompt → Coordinator 天然知道选谁、给什么 prompt、会拿回什么。`description` 不暴露内部 mode。

> **下次从这里继续**：开始 v2 实现第 1 步——agent 注册表 + `dispatch_agent` 工具（复用 core/tools.py 的 class-based 注册表风格）。v2 在原仓库 main 上推进（v1 已打 tag 并推 GitHub）。

---

## 九、2026-05-27/28：实现进度 + 接真 spoke 的整合设计

### 9.1 已实现（step 1-4，全离线测试绿，**尚未提交**，旧路由 OrchestratorAgent/intent.py 仍在、零破坏）
- **step 1 ✅** `core/registry.py`（`AgentSpec{name,description,input_contract,output_contract,factory}` + `AgentRegistry.get/names/catalog` + `build_default_registry()` 注册 `researcher`→ResearchAgent、`wiki_curator`→WikiAgent）+ `core/dispatch.py`（`DispatchAgentTool(Tool)` 复用 core/tools.py Tool ABC，查表→全新实例隔离运行→结构化 observation；永不向上抛）。验证 `tests/check_dispatch.py`。
- **step 2 ✅** `agents/coordinator_agent.py`（`CoordinatorAgent` ReAct 循环，工具表只放 dispatch_agent，退化谱 0/1/N，MAX_ROUNDS=10，最终 markdown 进 content + spokes_used 进 metadata）。验证 `tests/check_coordinator.py`。
- **step 3 ✅** 并行扇入：dispatch 加 `on_event` 回调；Coordinator 用 `asyncio.gather` 并发 + `asyncio.Queue` 扇入 + `spoke_id`（`agent#idx`）标签；过滤——只冒泡 log/tokens/stream/custom，status/result/error 不冒泡。
- **step 4 ✅** 串行依赖：**无生产代码**，是循环涌现属性（LLM 分轮：拿到上游 observation 再写下游 context）。加回归测试锁住 + 验证双向隔离（spy spoke 只收到自己的 prompt/context）。

### 9.2 关键认识修正（2026-05-28 讨论）
- **「hub 只拿摘要」是错的，是我误用了 −72% 教训。** −72% 讲的是**单个 agent 内部循环**别反复回灌膨胀的 transcript；它**不**意味着交给上级的**成品**要砍成摘要。导师类比：派你调研，你做了完整报告，却只给导师 3-5 句总结 = 荒谬。**hub 不要过程(trace)，但要完整成品(报告)。** 承重件 #2 原话其实分清了：「绝不回灌完整 **transcript**」≠「完整**产物**」。
- **完整报告 ≠ 完整资料**：researcher 搜 10 篇论文，是自己消化写成一篇**附来源的报告**，不是把 10 篇原文丢回来。
- **hub 是一个主体，不是两个**：Coordinator = 一个 agent = **LLM(大脑：判断/综合/写答复) + ReAct 循环代码(手：执行 tool_call、回填 observation)**。没有独立的 Python hub 背着大脑做文件 I/O。报告进 agent 上下文后，就是这同一个 agent 用上下文里的报告写最终答复，**不回读文件**。
- **持久化是 agent 自己的动作**：是否落盘由 **LLM 判断**（大脑决策），循环的手执行——和 dispatch_agent 同构。不是并行 Python 层偷偷自动存。

### 9.3 接真 spoke 的整合决策（待实现）
- **A（已定）**：ResearchAgent 先**最小可用**——默认 survey、截断摘要，不做自我 triage / LLM 自摘要（决策 C 精神，后续增强）。
- **B（已定）**：旧 OrchestratorAgent / intent.py / ChatAgent **先留着不动**，Coordinator 纯增量、用 --live 验证链路；链路稳了再单独做「换 AGENT_CLASS 入口 + 删旧」cutover。过渡期 ResearchAgent 同时认 `context` 和旧 `background_context`（cutover 时删）。
- **C → 演进为 (b)+(ii)（已定）**：
  - dispatch 的 observation 改成**带回 spoke 完整 result**（报告进上下文），不再是截断摘要。
  - 加 **`save_report` 工具**（Coordinator 第 2 个工具）：**是否落盘由 LLM 判断**。
  - save_report 用**引用来源**取内容——`save_report(source="researcher", topic=...)`，循环把**那次 spoke 的原始产出原样写盘**（保真、不让 LLM 重吐整篇导致又贵又失真）。代价：循环按来源暂存每次 spoke 的完整产出（大脑点名、手执行，非偷偷干活）。

### 9.4 重新拆步（每步独立可验证闭环）—— **下次从这里开始**
- **步 5** ResearchAgent 成 spoke：入参 `(prompt, context)`、默认 survey、返回**完整报告**（result.content）+ metadata `{status, summary}`、**不落盘**；同时改 dispatch observation 带回完整 result。离线验证（假 LLM+假检索器：observation 含完整报告 + metadata）。
- **步 6** WikiAgent 成 spoke：入参 `(prompt, context)`、`_resolve_input_paths` 用正则抠 `\S+\.md`、收尾 result 带 metadata `{status, summary(touched 页), key_facts}`。离线验证（temp wiki + 假 LLM 写页）。
- **步 7** `save_report` 工具 + 来源暂存：离线验证「派 researcher → save_report 引用它 → 落盘文件内容与 spoke 产出**逐字一致**」。
- **步 8** --live 真链路：给「调研RL最新进展，然后把报告归档进wiki」，断言 researcher → save_report → wiki_curator → 最终答复，报告落盘 + 归档成功。

> 顺手待办（与新架构正交但必须解）：#1 session_id、#2 async 文件 IO，留到 cutover 时一并处理。

---

## 十、2026-05-28（晚）：步5 设计敲定 + 步5.5 ResearchAgent 内部 ReAct 化

### 10.1 步5 三个分歧倒法（已定）
今天把步5具体到可下笔的程度。三个真分歧的判断：

| 分歧 | 决定 | 理由 |
|---|---|---|
| ① `summary` 来源（步5A 不允许 LLM 自摘要） | **取报告冒头一段（overview）** | report prompt 已经写「开头一段总览」，事实上的免费摘要；机械截断（`_snippet` 那样）失语义 |
| ② status 三态 vs 二态 | **拾起 degenerate**（survey 末尾聚合检测"所有子问题都空检索"时标 `degenerate`） | 档案 8.4 明确三态；失败标记现成在 research_agent.py:162（per-subquestion `continue`），末尾用 `all(...)` 聚合判定成本极小；focused 经路 196 不动（步5.5 删） |
| ③ focused 经路（paper_lookup/code_search）的 metadata | **不动，只给 survey 加** | intent 退役后 focused 是 dead code；YAGNI；且步5.5 会把它整个删掉 |

### 10.2 步5 实际变更点（小，半天活）

**ResearchAgent 侧**：
- `run(task, **kwargs)` 读 `kwargs.get("context")`（过渡期同时容忍旧 `background_context`，cutover 时删）。
- mode 默认 SURVEY 已经自然（`_normalize_mode(None)` → SURVEY），无需触碰。
- survey 的最终 `result` 事件加 `metadata={status, summary}`：summary = 报告 markdown 的第一段（首个空行前的内容），status 见 ②。
- 两条空检索早返回路径标 `status=degenerate`，summary 改成 `"(未检索到相关结果)"`。
- focused 经路保持原样不加 metadata。

**dispatch 侧**：
- `_run_isolated` 不再丢弃 `result_content`；`_format` 输出格式加一段 `report:\n<完整 markdown>`。
- `_snippet` 仍保留（spoke 没产出 metadata 时的 summary fallback 用）。

**测试**：新加 `tests/check_research_spoke.py`（假 LLM + 假 retriever）。断言 observation 同时含完整报告正文 + `status=ok` + summary 是冒头段。再加一个空检索用例断言 `status=degenerate`。

### 10.3 步5.5（新增）：ResearchAgent 内部 ReAct 化

**动机（递归 spoke 自决原则）**：现状 `ResearchAgent.run` 是 if/elif `mode` 分发器——把 hub 已经退役的硬编码路由复刻到了 spoke 内部。导师/学生类比落到代码：导师派学生调研，学生手里有「查论文 / 看 GitHub / 读特定 URL」工具，自己判断怎么用。

**对外契约不变**（步5锁定的接口：入参 `(prompt, context)`、出参 `result.content` 完整 markdown + `metadata{status, summary}`）。所以**步5 的测试在步5.5 后仍是绿的**——这是分两步做的本质价值。

**内部结构**：
- 复用 WikiAgent 的 ReAct 循环骨架（MAX_STEPS、错误转 observation、reasoning_content 回传、yield log）。档案三节"复用红利"就在指这里。
- 工具表（两层）：
  - 原子工具：`search_papers(query, max_results)`（Arxiv）、`search_web(query, max_results)`（Tavily）、`fetch_url(url)`（**新增**，轻量 HTTP 抓单页全文，给"读这篇 paper / 看这个 README"用）。
  - 复合工具：`do_broad_survey(topic)`（**保留现有 `_run_survey` 的多源并发批检索 + 去重，封成一个工具**）。
- system prompt 描述能力，**不再有 mode**——LLM 自己组织：宽调研可一次 `do_broad_survey`，也可多次 atomic search；找代码就 `search_web`；读特定论文 `fetch_url`。

**删的东西**：`_run_focused`、`_focused_query`、`_focused_report_messages`、`ResearchMode`（降级为彻底内部消失）。`_run_survey` 主体迁进 `do_broad_survey` 工具。

**风险**：内部 ReAct 比固定流水线多 token（带累计上下文+reasoning）。但 WikiAgent 已示范过 −72% 优化路径（[[wiki-index]] 代码侧投影），同套路可类比应用——不重灌过期资料。

> **下次接续**：先完成步5（小、独立闭环、可验证），再单独议步5.5（设计 → 编码）。步6/步7/步8 不动。
