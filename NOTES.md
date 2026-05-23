# 设计讨论存档

> 记录架构讨论和设计决策，按时间顺序追加，不修改已有内容。
> 详细历史（Retriever/ResearchAgent/生命周期钩子设计过程）：`docs/archive/phase1-phase2-history.md`

---

## 2026-05-21 — 架构缺口与长期演化方向

> 完整讨论见归档。此处保留对后续阶段仍有指导意义的结论。

### 三个结构性缺口（按优先级）

**缺口一：工具调用抽象层（阶段四关键阶跃）**

LLM 输出只有文本。意图识别、Retriever 选择、WikiAgent 写库等都需要 LLM 驱动工具调用。
不建立工具层，每个 Agent 手写 prompt + 输出解析，互不兼容。

待建：
```
core/tools/
├── base.py      # BaseTool(name, description, run(args) → str)
├── registry.py  # ToolRegistry
└── __init__.py
```
+ `BaseLLM.bind_tools()` 和 `chat_with_tools()` 对接 function calling。

**缺口二：跨请求共享状态层（阶段三正在填）**

每次 `agent.run(task)` 结束即销毁，无 Session 概念，无法跨对话接续上下文。
阶段三的 `core/session.py` 是这个缺口的最小化填补（会话窗口 + 文件持久化）。
未来更完整的方案：`core/memory/`（SlidingWindow / LLM压缩摘要）+ `core/storage/`（JSON→sqlite）。

**缺口三：单 Agent 单请求假设（阶段四+才需要）**

多 Agent 协作、后台常驻、用户中途取消等场景会破裂当前 WS/REST 模型。
`core/runner.py` 已落地生命周期管理，Orchestrator 层按需加，不提前设计。

### 长期演化路径

```
阶段三（当前）：连续对话（Session + IntentClassifier + OrchestratorAgent）
     ↓
阶段四：Tool 层（Function Calling + ToolRegistry）← 关键阶跃，在这里投入架构设计时间
     ↓
阶段五：Memory/Storage 层（长期记忆 + 持久化）
     ↓
阶段六：WikiAgent（= Storage 工具 + 管理型 Agent，此时顺水推舟）
     ↓
阶段七：Orchestrator（多 Agent 协作，按需加）
```

---

## 2026-05-21 — gpt-researcher 能力对比结论

> 完整对比表见归档。此处保留对路线决策仍有参考价值的结论。

### 核心差距（仍未填补）

| 维度 | 当前状态 | 重要性 |
|------|---------|--------|
| Web 真实抓取 | ❌（只有摘要/snippet） | 高：深挖 GitHub/文档时信息不足 |
| 上下文长度管理 | ❌ 全量拼接 | 高：报告长了会超 token |
| 来源质量筛选 | ❌ | 中：Tavily 结果质量不一 |
| 引用/参考文献 | ❌ | 中：学术报告无规范引用 |
| 研究深度 | 单层 | 低（阶段四工具层后可递归） |

### gpt-researcher 连续对话的局限

其 `ChatAgentWithMemory` 本质是"报告文本 QA"：报告塞入 system prompt + 历史消息，不触发新一轮多源检索，且每次请求重新 new（无跨 session 持久化）。

**结论**：agent-shell 的事件流 + session 架构比 gpt-researcher 更适合实现真正的交互式研究循环。

### 路线决策（已执行）

先做最小高价值项（Tavily 接入）再全力做阶段三，确保"做完阶段三时同时具备：能连续对话 + 能检索真实 Web"。✅ Tavily 已接入。

---

## 2026-05-21 — 阶段三设计讨论

### 三个关键决策

**意图识别方式：LLM-based**

拒绝规则/关键词方案，原因：用户提问方式多变，规则脆。LLM 方案方便后续扩展新意图分支。

**"深挖"定义**

深挖 = 新一轮完整 ResearchAgent（有新检索），携带上轮报告作 background_context。
不是"重新分析已有摘要"。场景包括：找相关论文、找 GitHub 实现、了解技术细节。

**记忆窗口 + 文件落盘**

最近 2 轮报告保有完整 content，超出窗口落盘至 `./reports/`，内存只保留元数据（路径 + 1-2 句摘要）。
按需加载：用户问到旧报告时从文件读取（接口已设计，阶段三先不实现）。

### 实现计划

完整设计见 CONTEXT.md §五。

---

## 2026-05-22 — ResearchAgent 职责边界 + 意图模型重构（思考一）

> 起点：反思 ResearchAgent 该不该有"记忆"和"自主判断能力"。结论改写了阶段三的意图模型。

### 先分清两种记忆

| | 跨轮记忆（会话） | 任务内工作记忆（scratchpad） |
|---|---|---|
| 内容 | 上轮报告、对话历史、话题 | 本次研究中已搜过/搜到了什么、下一步搜什么 |
| 生命周期 | 跨多轮对话 | 单次 run() 内，结束即销毁 |
| 归属 | 编排层（Session 持有） | 仅当 agent 要自主决策时才需要 |

`background_context` 不是给 ResearchAgent 记忆——是编排层把上轮报告当**参数**传下来，agent 收完即用、依然无状态。"接收 context" ≠ "拥有 memory"。

### 固定流水线 vs 自主判断

现状 ResearchAgent 是**固定流水线**（拆 3 子问题→检索→总结→报告，结构写死），LLM 只做内容判断不做控制流判断。这是个**主动选择**，对复现学习项目是合理的 level 0（gpt-researcher 主体也是固定 pipeline）。

**决策：ResearchAgent 保持无状态固定流水线，但做成"可选的流水线"——由调用层用参数（mode）控制分支。** 不引入任务内工作记忆 / ReAct 循环（那是潜在"阶段X：执行自主权"的独立主题）。

### 核心判据（可反复套用）

> **一块判断该放哪一层，取决于哪一层能看见它依赖的状态。**
> 跨轮上下文/用户意图 → 编排层。任务内检索结果/检索器实现细节 → agent 自己。

战略/路由判断（新研究？深挖？追问？什么 mode？）依赖用户 NL + 对话历史 → 上层。
战术/执行判断（搜空了重写查询、够不够、深挖哪点）依赖中间检索结果 → 必须留在 agent 内；硬上交会让编排层变成研究循环本体，分层塌掉。本阶段不做战术自主。

### "做什么 / 怎么做"切缝

- **上层下达声明式指令**：`mode` + `target`（如 `mode=code_search, target="Attention Is All You Need"`）。
- **ResearchAgent 自己翻译成检索机制**：该 mode 用哪些检索器、查询词怎么拼、要几条、输出什么形状。

上层不该知道"找 GitHub 代码"其实走 TavilyRetriever 加 site 过滤（那是检索层实现泄漏）；agent 也不该只拿 task 字符串去重做一遍上层已做过的意图分类。中间这条缝最干净。

### 意图模型重构：删 refine，拆成三个正交轴

`mode` 引入后，`refine` 作为意图标签冗余了——但不能直接删，否则丢信号。refine 原本捆了两件事：①"这是检索任务"（= research，路由到 ResearchAgent）②"接着上轮话题"（带 background_context）。`mode` 只吸收了①里"搜什么形态"的部分，没吸收②。

**正确动作：把 refine 拆成正交字段，不是让 mode 吞掉它。** 否则保留 refine + 加 mode 会语义组合爆炸（research+survey / research+code / refine+survey / refine+code…）；拆成正交轴正是给爆炸泄压，也更好扩展。

三个正交轴：

| 轴 | 取值 | 决定什么 | 归属/依赖 |
|---|---|---|---|
| **route** | research / chat | 要不要新检索（ResearchAgent vs ChatAgent） | 上层 |
| **mode** | survey / paper_lookup / code_search | ResearchAgent 内部检索广度 + 输出形状（仅 research 时有意义） | 上层下达，agent 执行 |
| **carry_context** | bool | 带不带上轮报告作背景（相关性判断依赖对话状态，必须上层判，默认全注会污染新研究） | 上层 |

意图分类器输出：`{route, mode, target, carry_context}`，仍一次 LLM 调用全出。

原三场景映射（新方案更可表达，如"全新话题但找代码" = research + code_search + carry_context=false，旧方案表达不了）：

| 场景 | route | mode | carry_context |
|---|---|---|---|
| ① 追问"X 是什么意思" | chat | — | true |
| ② 深挖"找 Y 的 GitHub 实现" | research | code_search | true |
| ③ 新研究"换个话题研究 Z" | research | survey | false |

### 待盯的代价

mode 枚举成了跨层共享词表（新耦合面）。需显式命名这条缝：一个 directive 对象 + 一个双方 import 的 mode 枚举。3 个 mode 没问题，将来 mode 爆炸时才是考虑给 agent 战术自主权的信号。

### carry_context 粒度

先做 **bool（带最近一份报告）**，覆盖当前场景；"带哪几份"（Session 有窗口）留作边界。

---

## 2026-05-22 — IntentClassifier 扩展性 + OrchestratorAgent 职责（思考二）

### IntentClassifier 的扩展性：加 WikiAgent = 加一条 route 臂

route 做成正交轴的回报是**开闭原则**：加新功能 = 加一条 route 臂，不动已有 research/chat。但"加一条 route 即可"略低估——实际要动三处，架构不变：

1. route 枚举加值（如 `wiki`）
2. **它带的 payload 不同**：mode/target/carry_context 是 research 专属；wiki 操作（沉淀/查询/更新知识库）有自己的参数形状（如 `{operation, entry}`）。→ `IntentResult` 早晚从扁平结构演化成**按 route 分臂的 tagged union**
3. 分类器学会区分新 route + 编排层加 dispatch 分支

判据校验：「wiki 操作 vs research 请求」依赖用户 NL + 历史 → 归 IntentClassifier，分层不破。

**结论**：扩展点干净隔离，但是"加一整条臂（值+payload+示例+dispatch）"，不是"加个枚举值"。research/chat 两臂时 IntentResult 保持扁平，**等 wiki 真来了再重构成 tagged union，不提前抽象**。

### OrchestratorAgent：意义 / 职责 / 边界 / 记忆范围

**为什么存在（一句话）**：worker 保持无状态的代价是每次 run() 失忆。Orchestrator 的唯一存在理由 = 把"一串无状态 worker 调用"缝成"一段连贯对话"，是跨轮状态的家（导演；Session 是导演笔记本）。worker 无状态 ↔ Orchestrator 持有状态，是共生设计。

**职责（5 项）**：
1. 持有 Session / 维持连续性（class-level SessionManager 单例，key=id(websocket)）
2. 触发意图分类（调 classify_intent；逻辑住 core/intent.py，它只管"何时调、喂什么上下文"）
3. 路由 / dispatch（按 route 选 worker，按字段传 kwargs）
4. 上下文组装（carry_context=true 时从 Session 取报告塑形成 background/context 注入）
5. 写回 Session（add_turn；产出报告则 save_report → 窗口+落盘）

**明确不做**：
- 不 emit status/error（它自己被 run_agent() 包，由 runner 管）
- 调 worker 用 `.run()` 直连、不过 run_agent()（避免嵌套 status），只转发 worker 的领域事件 + 可加自己的 log
- 不做检索/写报告/WS 管理/worker 内部战术决策
- 唯一 LLM 用途是意图分类。**薄协调器，不是思考者**

**记忆范围：当前单个 session 的全部，但有保真度梯度**
- Turn 全保留（文本小）；报告最近 N 份保全文，更老落盘只留元数据（摘要+路径），按需加载
- 故意窗口化，给 token 成本设上界
- 不跨 session（多 session 按连接区分，一次只读写当前）；不超连接生命周期（WS 重连=新 session）
- **关键澄清**：Orchestrator 实例本身无状态，记忆在 class-level SessionManager。准确说法是"无状态协调器操作一个持有记忆的 SessionManager"。class-level 单例正是为了让状态独立于实例生命周期存活。

**命名陷阱**：阶段七的 Orchestrator（多 agent 协作）远比阶段三宽。阶段三 OrchestratorAgent 当前约束是「**一轮 → 分类 → 路由到一个 worker → 写回**」，单 worker，不做并行/委派图。是未来的种子，别把阶段七想象提前压进来。

---

## 2026-05-22 — WikiAgent 愿景与形态（讨论一，细节未定，下次续）

> 阶段三已完成。开始讨论 WikiAgent。本节是阶段性存档，部分细节用户还没想清，下次会话继续。

### 用户的愿景（明确部分）

WikiAgent = **增量式构建并维护一个持久化 Wiki**——一套结构化、相互链接的 Markdown 文件集合，**LLM 负责撰写和维护所有内容**（归纳、交叉引用、归档、保持全局一致性等全部繁琐维护工作）。

- **LLM 生成的页面类型**：摘要、实体页面、概念页面、对比分析、概览、综合论述。
- **LLM 完全拥有这一层**：创建页面、新资料到来时更新、维护交叉引用、保持全局一致性。
- **Schema 图式层（核心）**：一份文档（类比 Claude Code 的 CLAUDE.md / Codex 的 AGENTS.md），告诉 LLM Wiki 的结构规范、约定、以及摄入/问答/维护时的工作流程。它让 LLM 成为**有纪律的 Wiki 维护者**而非普通聊天机器人。用户与 LLM 随时间**共同演化**这份文档，摸索领域最佳实践。
- **触发方式**：显式触发，由用户发起。
- **设计哲学**：智能放在 prompt + Schema（散文/数据）里，**代码极薄**——代码只给 LLM 读写/浏览文件的工具 + Schema，由 LLM 自己决定建哪些页、改哪些页、修哪些链接。

### 三个关键含义（Claude 提出，已与用户基本对齐）

1. **WikiAgent 是项目第一个真正 "agentic" 的 agent**：必须「读 Schema → 环顾(list/read 已有页) → 决策 → 写多文件 → 校验修复交叉引用 → 迭代」，是**工具调用循环（ReAct 式）**，不是单 prompt→单输出。它正是「思考一」里推迟的"**执行自主权**"，只是动机是维护知识库而非研究。

2. **Tool 层 + function calling 是内在前置（强需要）**：已核实 `core/llm/base.py` 只有 `chat`/`chat_stream`，无工具调用（Role 里预留了 `"tool"`）。要做真正的 WikiAgent，需先有最小 Tool 层（`read_file`/`write_file`/`list_files`/可能 `grep`，沙箱限定 `./wiki/`）+ BaseLLM 支持 tool-calling 循环。

3. **wiki 文件本身就是持久层**：不一定需要独立 DB Storage 层；markdown 语料库 + Schema 就是存储。session 的 Memory 层与 wiki **正交、对 wiki 非必需**。→ 修正 NOTES 演化路径里"wiki 依赖阶段五 Storage/Memory"的假设：**tool 强需要，session-memory 不需要**。用户原话"可能 tool 和 memory 都需要，到时按实际情况定"。

### 仍待厘清（下次从这里继续）

1. **摄入输入源**：(a) 当前 session 最近那份研究报告 / (b) 用户指定主题让 wiki 去消化 / (c) 粘贴任意文本。Claude 猜主要是 (a)，未确认。
2. **一致性维护边界**：全库重扫不 scale。v1 倾向有界策略（只动相关实体/概念页 + 索引页）。用户对 v1 维护雄心多大？未定。
3. **Schema v0**：是否现在一起起草 `./wiki/SCHEMA.md`（页面类型、`[[wikilink]]` 约定、ingest/query/maintain 工作流）。待定。
4. **query 行为**：返回页面原文 vs LLM 综合作答；以及与现有 `chat` 路由的区分（chat=易逝的当轮报告问答，wiki query=持久知识库问答，靠分类器措辞区分）。
5. **写操作安全**：限定 `./wiki/` + git/备份兜底 + 必要时 review，防 clobber。

### 接入方式（早前「思考二」已定）

加 WikiAgent = IntentClassifier 加一条 `route=wiki` 臂（带自己的 payload，如 `{operation: ingest/query/maintain, ...}`）+ 分类示例 + dispatch，不动 research/chat。route 增多且各带不同 payload 时把 IntentResult 重构成 tagged union。
