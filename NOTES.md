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

---

## 2026-05-23 — WikiAgent 形态细化（讨论一补完）

> 在「愿景与形态」基础上把 ingest 时的知识组织方式定死。实现快照见 CONTEXT.md。

**wiki 页面是「主题中心」而非「文件镜像」**：页面不是输入文件的 1:1 拷贝，而是以知识主题为中心的积累点。多个来源文件可汇聚到同一页面（同一主题的多次学习）；一个跨主题文档可拆到多个页面。LLM 自主决定归属，不做强制单一分类。

**index.md 是脊梁**：面向内容的目录，每页一条、按类别组织，每次 ingest 时更新。两个作用——LLM 的环顾入口（ingest 先读它了解现有结构），未来 ChatAgent 的检索入口。它同时是 **category 的权威清单**，用来压制同义类别发散。

**相关性判断分两阶段**（思路，不是固定流水线）：① 用实体集合交集从 index 圈候选页（无 LLM）② 对候选批量打分（0-1 + 理由）。**关键纠错**：不要让 LLM "判断这份资料属于哪个主题"——强制单一归类必然出错；要的是"这份资料对这个页面的增益有多大"。打分逻辑是对的，但在 agentic 设计里它退化成"LLM 读 index、眼判实体重叠、读候选页、判断是否实质丰富"，不强制产出数字分数。

**为何必须真正 agentic（最重要结论）**：执行序列依赖运行时发现的信息——读几个页、写几个页、新建几个页，全由"读到的 index/页面内容与新文档的关系"决定，Python 无法预知。对比 ResearchAgent 的固定流水线是合理的 level 0；WikiAgent 若也用固定流水线 + 写死阈值，控制权就从 LLM 转回程序员，agentic 消失。这正是「思考一」里推迟的"执行自主权"，动机是维护知识库。

---

## 2026-05-23 — BaseLLM tool calling 抽象（讨论三）

> 第一个真正 agentic 的 agent 的硬前置：BaseLLM 之前只有 chat/chat_stream，无 tool calling。

**最核心判断：ReAct 循环放在 WikiAgent，不在 BaseLLM。** BaseLLM 只做"单次 tool-enabled 调用"的归一化。理由：① agentic 的控制权（读几页/写几页/何时停）必须在 agent；② 可观测性要求每步 yield log，BaseLLM 吞循环就看不到过程；③ 每步 token 记账/工具错误/恢复都是 agent 关切；④ 与现有 chat 单发语义一致。

**中性数据模型**：`ToolSpec`（name/description/parameters JSON Schema）、`ToolCall`（id/name/arguments，arguments 在 provider 里把 OpenAI 的 JSON 字符串解析成 dict）；`LLMResponse` 加 tool_calls / stop_reason。agent 只碰中性类型，永不接触 provider 原生结构。

**最漏的缝 = 消息历史往返**：OpenAI 把 tool_calls 挂在 assistant 消息、结果是独立 `role:"tool"` 消息；Anthropic 把 tool_use 放进 content block 数组、结果必须合并进一条 user 消息的多个 tool_result block。解法：`ChatMessage` 加可选 tool_calls/tool_call_id，序列化知识下沉到各 provider（不再假设 to_dict() 通用）。Anthropic 的"连续结果合并"是最易写错点。

**范围裁剪**：工具路径不做流式（后台策展不需要逐 token，流式拼接 tool_calls 痛且无收益）；先只实现 DeepSeek/OpenAI，Anthropic 设计成可插入但本期 NotImplementedError。**签名扩展 `chat(tools=...)`** 而非新增 chat_with_tools（同一单发语义，改动面小）。

**实现中踩的坑（思考模型 reasoning_content）**：deepseek-v4-pro 是思考模型，返回 tool_call 时附带 `reasoning_content`，续接对话必须原样回传否则 400。解法：中性类型加一个不透明可选字段 reasoning_content，provider 读写、其它 provider 忽略——比"存 raw 重放"干净。对 ReAct 循环是硬约束：追加 assistant 工具调用轮时必须带上它。

---

## 2026-05-23 — 工具层 + ./wiki/ 沙箱（讨论四）

**工具抽象：显式 class-based + 手写 schema**（不用装饰器签名推断）。只有 3-4 个工具、schema 极小，手写让"喂给 LLM 的契约"一眼可见；装饰器推断是藏行为的 magic，违背裸 SDK/代码薄/重可观测的气质。注册表对循环暴露 `specs()` + `execute(call)→str`。

**沙箱集中一处**：`_resolve()` 用 `Path.resolve()` + `is_relative_to(root)`，挡路径穿越/绝对路径/符号链接逃逸。安全逻辑不能散落到各工具。

**错误处理（最关键哲学）：工具永不向循环 raise，所有失败都收敛成 observation 字符串回喂 LLM。** 文件不存在/越界/坏参数不是 bug，是信息——LLM 看到错误自己改路径、改策略。越界在 _resolve 内部 raise、registry 边界 catch 成字符串（且越界操作绝不执行）。推论：唯一能停循环的是"无 tool_call"或 MAX_STEPS——这把循环边界收窄了。

**工具集**：先做 read_file/write_file（整篇覆盖+自动建父目录+限定 .md）/list_files（递归、相对路径）；grep 推迟（index.md 本就是检索入口，不需全文检索）。**冷启动**由沙箱种 skeleton index.md，免得空库时 list/read 返回令人困惑的空。

---

## 2026-05-23 — SCHEMA.md 设计（讨论五）

SCHEMA 是 ReAct 循环启动时塞给 LLM 的操作手册，按设计哲学"智能在散文里、代码极薄"，质量基本决定 WikiAgent 好不好用。**放 system prompt**（不放 ./wiki/ 让 LLM 用工具读）——规范要始终在场、不浪费循环步数。

4 个分叉（均取轻量 agentic 一侧）：
1. **相关性不强制数字分数**，改定性纪律 + 写每页前一句话理由（保 agentic、省 token、给可观测性）。阈值作引导不硬算。
2. **index.md 最后统一更新一次**（单次 ingest 是事务单元，省步数）；"index 更新完 = 结束"正好给循环一个 STOP 信号。代价：中途崩溃留下不一致，本期可接受。
3. **category LLM 自创但强制先读 index 优先复用**，用 index 当权威清单压制同义发散。
4. **合并策略**：新知识整合进对应小节（非末尾粗暴 append），摘要重写覆盖全部来源，来源追加，entities 合并去重。

**头号硬纪律**：更新页面前必须先 read 再 write——write_file 是整篇覆盖，不先读就写会毁掉整页已有知识。

---

## 2026-05-23 — ReAct 循环兜底（讨论六）

工具层已保证不抛、错误即 observation，所以兜底只需管"循环本身怎么收尾、怎么不失控"。

- **MAX_STEPS = 20**（每次 ingest 的 LLM 调用上限）。命中即停 + 列已写文件 + 警告，不自动修 index（无事务），不做强制总结调用。
- **兜圈子检测（采纳"加 nudge"）**：同一 (name,args) 重复 ≥3 次 → 注入一次 nudge（"你已多次得到相同结果，请改变策略或结束"）；硬截仍归 MAX_STEPS。一句 nudge 成本极低、常能把 LLM 救回来。
- **result 产出**：自然停止 → LLM 末轮 text content 即收尾总结；强制停止 → 用 touched_files 合成。不额外发"请总结"调用。
- **"写错"不兜语义**：路径越界/非 .md 由工具层返 error、LLM 自纠；语义写错（A 主题塞进 B 页）工具层无法判断，靠 SCHEMA 纪律 + LLM 自律，不做程序兜底（避免过度工程）。

**已知边界（与 B9 同源）**：冷启动首轮无 session 上下文时 classify 跳过 LLM 降级 research，故"首条消息就归档 wiki"会落到 research；自然流程"先研究→再归档"有上下文则正常命中。修法：无上下文时仍调一次 LLM（route/files 可只凭输入判定），留作可选改进。
