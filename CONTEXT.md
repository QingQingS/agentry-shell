# 项目上下文

> 用于对话重启时快速恢复进度。
> 最近更新：2026-05-23
> 历史开发记录：`docs/archive/phase1-phase2-history.md`

---

## 一、项目状态

**目标**：分阶段构建 LLM Agent 系统，通过复现理解各层如何搭建（参照 gpt-researcher）。

```
阶段一 ✅  基础架子：FastAPI + WebSocket + CLI + Web UI + AgentInterface
阶段二 ✅  研究功能：LLM层 / Retriever层 / ResearchAgent / 流式输出 / Tavily / 多源并发
阶段三 ✅  连续对话：session/intent/agent mode/orchestrator 全部实现，CLI 多轮 E2E 跑通（survey→chat→code_search，连续性+代词消解+落盘验证）。前端 B4 已修（追加式多轮历史），WS 单连接多轮已验证
阶段四 ✅  WikiAgent（持久化 LLM 策展 Wiki，项目首个 agentic agent）——设计存档见 wiki-agent开发.md（七~十节）。Step A 中性类型 / B 工具路径（含思考模型 reasoning_content 回传坑）/ C core/tools.py 工具层+沙箱 / D agents/wiki_agent.py ReAct 循环+wiki_schema.py / E 接入 Orchestrator（intent 加 route=wiki 臂+files 字段，dispatch 透传 files）全部完成。离线全绿 + 真实 DeepSeek 跑通（WikiAgent 端到端归档；分类器 live 正确判 wiki）。
            ⚠ 已知边界：冷启动首轮（无 session 上下文）classify 跳过 LLM 降级 research，故「首条消息就归档 wiki」会落到 research（与 B9 同源）；自然流程「先研究→再归档」有上下文则正常命中 wiki。
```

---

## 二、当前架构

```
agent-shell/
├── core/
│   ├── config.py              # 配置（env > .env > defaults）
│   ├── agent_interface.py     # AgentInterface + AgentEvent + 生命周期契约
│   ├── runner.py              # run_agent()：统一 status/error/钩子
│   ├── stream.py              # ⚠ 死代码（待删）
│   ├── llm/
│   │   ├── base.py            # BaseLLM / ChatMessage / LLMResponse / TokenUsage
│   │   ├── openai_provider.py # OpenAIProvider + DeepSeekProvider
│   │   ├── anthropic_provider.py
│   │   ├── factory.py         # get_llm(tier, config)
│   │   └── __init__.py
│   └── retrievers/
│       ├── base.py            # BaseRetriever + SearchResult
│       ├── arxiv.py           # ArxivRetriever（asyncio.to_thread + 429 重试）
│       ├── local_file.py      # LocalFileRetriever（txt/md/pdf，关键词评分）
│       ├── tavily.py          # TavilyRetriever（httpx，TAVILY_API_KEY）
│       └── __init__.py
├── agents/
│   ├── echo_agent.py          # 演示用，无 LLM
│   ├── chat_agent.py          # 单轮聊天
│   ├── research_agent.py      # 多源并发检索 + 流式报告
│   ├── orchestrator_agent.py  # ⏳ 待实现：连续对话编排中枢
│   └── __init__.py            # ⚠ 自动 import EchoAgent（埋雷，待清空）
├── backend/server/
│   ├── app.py                 # FastAPI 路由
│   └── websocket_manager.py   # WS 连接管理 + session token 累计
├── frontend/                  # HTML / JS / CSS
├── docs/archive/              # 历史设计记录（只查不改）
├── cli.py
├── main.py
├── pyproject.toml
└── .env                       # API Keys（不入 git）
```

---

## 三、关键接口约定

### AgentEvent 协议

```
type 值：
  "log"     → 过程日志（显示在前端 log 区）
  "stream"  → 流式文本增量（逐 token，配合 result 使用）
  "result"  → 最终完整结果
  "status"  → running / done / error（由 runner.py 统一 emit，Agent 内不 emit）
  "tokens"  → metadata: {input/output/total_tokens, provider, model, scope?}
  "error"   → 错误信息（由 runner.py 统一 emit）
```

**Agent 契约**：`run()` 只 yield 领域事件（log/stream/tokens/result），失败时抛异常；status/error 由 `core/runner.py run_agent()` 统一包装。

### BaseLLM 核心方法

```python
await llm.chat(messages)           → LLMResponse
async for chunk in llm.chat_stream(messages)  → str 增量（结束后自动 _record_usage）
llm.cumulative_usage               → TokenUsage（该实例累计用量）
```

### 检索源配置

```bash
RETRIEVER=arxiv          # 学术论文，无需 Key
RETRIEVER=tavily         # 实时网页，需 TAVILY_API_KEY
RETRIEVER=arxiv,tavily   # 两者并发，结果交叉去重（当前默认）
```

---

## 四、运行方式

```bash
cd /Users/sunqingqing/projects/agent-shell
PY=/usr/local/Caskroom/miniforge/base/envs/claude-deepseek/bin/python

$PY cli.py "任务"          # 单次执行
$PY cli.py --interactive   # 交互式多轮（阶段三后支持会话记忆）
$PY main.py                # Web 服务 → http://localhost:8000
```

**当前 `.env` 关键配置**：

```
LLM_PROVIDER=deepseek
SMART_LLM_MODEL=deepseek-v4-pro
FAST_LLM_MODEL=deepseek-v4-pro
DEEPSEEK_API_KEY=sk-9b8169...
AGENT_CLASS=agents.chat_agent.ChatAgent   # 阶段三改为 OrchestratorAgent
RETRIEVER=arxiv,tavily
TAVILY_API_KEY=tvly-dev-...
```

---

## 五、阶段三实现计划（连续对话）

> 设计已完成，每步是独立可执行闭环，按序实现。

### 5.1 目标场景

> 2026-05-22 更新：意图从单一 `intent` 改为三个正交轴 `route / mode / carry_context`（详见 NOTES.md「思考一」）。

```
                                              route     mode          carry_context
① 追问   "刚才报告里说的 X 是什么意思？"        chat      —             true   → ChatAgent 带上轮报告
② 深挖   "帮我找找 Y 相关的 GitHub 实现"         research  code_search   true   → ResearchAgent 带背景
③ 新研究 "换个话题，研究 Z"                      research  survey        false  → ResearchAgent 无背景
④ 单篇   "讲讲 Attention Is All You Need"        research  paper_lookup  false  → ResearchAgent 单目标
```

### 5.2 架构

```
现在：用户输入 → AGENT_CLASS → run_agent() → 事件流

之后：用户输入 → OrchestratorAgent（新 AGENT_CLASS）
                    ├── 读 Session（最近 N 轮报告 + 对话历史）
                    ├── IntentClassifier（fast LLM）→ {route, mode, target, carry_context}
                    │     route=research → ResearchAgent(mode, target, background_context=上轮报告 if carry_context)
                    │     route=chat     → ChatAgent(context=上轮报告 if carry_context)
                    └── 结果写回 Session（满窗口则落盘 ./reports/）

意图三轴（正交，详见 NOTES.md 2026-05-22「思考一」）：
  route ∈ {research, chat}                          要不要新检索 → 选哪个 Agent
  mode  ∈ {survey, paper_lookup, code_search}       仅 research 有意义 → ResearchAgent 内部分支
  carry_context: bool                               带不带上轮报告作背景（先做最近一份）
```

**关键约束**：
- `OrchestratorAgent._session_manager` 是 class-level 单例，key = `id(self.websocket)`
- CLI `--interactive`：`websocket=None`，`id(None)` 全进程恒定 → 多轮天然有效
- Orchestrator 直接调子 Agent `.run()`，**不过** `run_agent()` 包装（避免嵌套 status）
- 不改 WebSocketManager / app.py / runner.py

**OrchestratorAgent 职责与边界**（详见 NOTES.md 2026-05-22「思考二」）：

存在意义：worker 无状态 → 每次 run() 失忆；Orchestrator 把"一串无状态 worker 调用"缝成"连贯对话"，是跨轮状态的家。两者共生。

5 项职责：① 持有 Session/维持连续性 ② 触发意图分类（逻辑在 core/intent.py，它只管何时调、喂什么上下文）③ 按 route 路由 dispatch ④ carry_context 时从 Session 取报告塑形注入 ⑤ 写回 Session（add_turn / save_report）。

不做：不 emit status/error（runner 管）；只转发 worker 领域事件；不做检索/写报告/WS 管理/worker 内部战术决策；唯一 LLM 用途是意图分类——薄协调器，不是思考者。

记忆范围：**当前单个 session 全部**，保真度梯度（Turn 全留；报告近 N 份全文、更老落盘留元数据）。不跨 session、不超连接生命周期（WS 重连=新 session）。注意：Orchestrator 实例本身无状态，记忆在 class-level SessionManager。

当前约束：一轮 → 分类 → 路由到**一个** worker → 写回（单 worker；多 agent 协作/并行是阶段七，不提前做）。

**扩展性**：加新功能（如知识管理 WikiAgent）= 加一条 route 臂（route 值 + 该 route 的 payload + 分类示例 + dispatch 分支），不动 research/chat。research/chat 期 IntentResult 保持扁平；route 增多且各带不同 payload 时再重构成按 route 分臂的 tagged union（不提前抽象）。

### 5.3 Session 数据结构（core/session.py）

```python
@dataclass
class ReportRecord:
    topic: str
    description: str        # fast LLM 生成的 1-2 句摘要（供意图分类参考）
    file_path: str          # ./reports/{session_id}/{ts}_{slug}.md
    content: Optional[str]  # 窗口内有值；超出窗口置 None
    timestamp: str

@dataclass
class Turn:
    user_input: str
    agent_response: str     # result 事件内容
    intent: str             # "research" / "refine" / "chat"
    timestamp: str

@dataclass
class Session:
    session_id: str
    turns: List[Turn]           # 全部保留（文本小）
    reports: List[ReportRecord] # 最近 WINDOW_SIZE=2 份保有 content
```

`SessionManager` 关键方法：`get_or_create` / `save_report`（写文件+窗口管理）/ `add_turn` / `get_recent_context`（返回最近报告+对话摘要）

### 5.4 意图分类器（core/intent.py）

> 2026-05-22 重构：删除 refine，拆成正交三轴。原 refine 的工作分摊给 route(=research) + carry_context。

```python
ResearchMode = Literal["survey", "paper_lookup", "code_search"]

@dataclass
class IntentResult:
    route: Literal["research", "chat"]   # 要不要新检索
    mode: ResearchMode                   # 仅 route=research 有意义；chat 时取默认值/忽略
    target: str                          # 可直接检索的查询词/目标；chat 时可为空串
    carry_context: bool                  # 带不带上轮报告作背景

async def classify_intent(user_input, session_context, llm) -> IntentResult
```

- 一次 LLM 调用完成：route + mode + target + carry_context（避免多次调用）
- 输出严格 JSON：`{"route": "research", "mode": "code_search", "target": "xxx GitHub implementations", "carry_context": true}`
- 无上下文时跳过 LLM，直接返回 `route=research, mode=survey, carry_context=false`
- 解析失败降级为 `route=research, mode=survey, carry_context=false`
- mode 枚举（ResearchMode）是跨层契约：定义在 `agents/research_agent.py`（agent 拥有自己的能力词表），`core/intent.py` 反向 import

### 5.5 上下文注入 + mode 分支

**carry_context 门控背景注入**（route 无论 research/chat 都受此控制）。

**ChatAgent（route=chat）**：`kwargs["context"]` → 注入 system prompt 作为背景（carry_context=true 时才传）

**ResearchAgent（route=research）**，接收两类参数：

1. `kwargs["mode"]` → 内部映射表决定检索机制与输出形状：
   - `survey`：启用全部配置检索器 + 多子问题拆解 + 结构化报告（= 当前默认行为）
   - `paper_lookup`：聚焦单目标（arxiv 向），不拆多子问题，输出单篇综述
   - `code_search`：web/github 向检索器，输出 repo/实现信息列表
   - 「该 mode 用哪个检索器/查询词/条数」是 agent 内部知识，上层不指定
2. `kwargs["background_context"]`（carry_context=true 时才传）→ 注入两处：
   - `_decompose_messages`：子问题聚焦于背景未覆盖的角度
   - `_report_messages`：报告参考已有结论，不重复

### 5.6 实现步骤

| 步骤 | 文件 | 核心工作 | 验证方式 |
|------|------|---------|---------|
| ✅ Step 1 | `core/session.py` | SessionManager + 文件落盘 + 窗口管理（WINDOW_SIZE=2，get_recent_context 返回结构化 RecentContext） | `tests/check_session.py` 全绿（已完成） |
| ✅ Step 2 | `core/intent.py` | IntentClassifier 输出 `{route, mode, target, carry_context}`（LLM+JSON+降级）。**ResearchMode 枚举定在 research_agent.py，intent 反向 import** | `tests/check_intent.py` 离线全绿 + `--live` 4 场景人工确认通过（含代词消解） |
| ✅ Step 3 | `research_agent.py` `chat_agent.py` | ResearchAgent 加 mode 分支（最小：survey=现行为；paper_lookup/code_search=单查询+针对性报告）+ background_context；ChatAgent 加 context | `tests/check_agents.py` 离线全绿（检索器选择/prompt 注入/context 拼接） |
| ✅ Step 4 | `orchestrator_agent.py` | class-level Session + 三轴路由（route 选 Agent，mode/target/carry_context 透传）+ 写回。description 暂用正文快照（见 §六待办） | `tests/check_orchestrator.py` 离线全绿（路由/kwargs/转发/写回/carry 门控/隔离） |
| ✅ Step 5 | `.env` | 已切 `AGENT_CLASS=agents.orchestrator_agent.OrchestratorAgent` | CLI `--interactive` 三轮 E2E（survey→chat→code_search）真实跑通：连续性 + 代词消解 + ./reports/ 落盘正确；WS 路径确认注入 websocket（每连接隔离）；服务启动冒烟 200。**浏览器 UI 未点击实测**（受前端 B4 影响，留待前端重构） |

依赖顺序：1 → 2 → 3 → 4 → 5（2 和 3 可并行）

### 5.7 已知边界（暂不做）

- 窗口外旧报告的自动按需加载（接口已留）
- 报告过长超 token 限制（先观察）
- WebSocket 重连后 session 丢失（重连视为新会话）
- REST `/api/run` 不支持多轮

---

## 六、待处理事项

### 活跃 Bug

| # | 位置 | 问题 | 工作量 |
|---|------|------|--------|
| ~~B4~~ | `frontend/scripts.js` | ✅ 已修：改为追加式轮次块，每轮在日志区/结果区各追加一块，历史在 session 内累积；仅新 WS 连接（含重连=新 session）或手动清空时清。WS 单连接多轮已验证共享 session | — |
| B6 | `agents/research_agent.py` | 检索结果未透传到前端 log（论文标题/作者不可见） | 小 |
| B7 | `agents/research_agent.py` | 研究简报缺逐篇论文简介，直接跳综合结论 | 小 |
| B8 | `agents/orchestrator_agent.py` | `description` 暂用报告正文前 120 字快照。后期 ResearchAgent 升级为结构化 JSON 输出后，摘要应由 ResearchAgent 产出、Orchestrator 只负责存（避免在编排层多调一次 LLM、放错层） | 小 |
| B9 | `core/intent.py` | 首轮无上下文时 `classify_intent` 跳过 LLM、固定降级为 research/survey/carry=false。导致"首条消息就是单篇/找代码"无法命中 paper_lookup/code_search（route/carry 确实需上下文，但 mode 本可只凭输入判断）。可选改进：无上下文时仍调 LLM 只定 mode | 小 |

### 活跃埋雷

| 级别 | 位置 | 问题 | 修法 |
|------|------|------|------|
| 🔴 P1 | `agents/__init__.py` | 自动 import EchoAgent，新 Agent 引入三方包时会炸 | 清空，靠 AGENT_CLASS 动态加载 |
| 🔴 P1 | `POST /api/run` | 同步阻塞，ResearchAgent 长任务超时 | 文档明示"长任务走 WS"或改 202+task_id |
| 🟡 P2 | `core/stream.py` | `stream_output()` 死代码 | 删除 |
| 🟡 P2 | `websocket_manager.py` | `active` 字典无用 | 删除或加注释 |
| 🟡 P2 | `cli.py` + WS | `load_agent()` 重复 + CLI 缺 issubclass 校验 | 提到 `core/loader.py` |
| 🟡 P2 | 全局 | 无任何测试 | 至少加 `tests/test_agent_protocol.py` |

---

## 七、启动提示词

```
我正在开发 /Users/sunqingqing/projects/agent-shell。
请读取 CONTEXT.md 了解项目背景和进度。

阶段一、二全部完成。阶段三（连续对话）设计已完成，下一步从 Step 1 开始实现：
  Step 1: core/session.py — 会话记忆 + 报告文件落盘
  Step 2: core/intent.py — LLM 意图分类（research/refine/chat）
  Step 3: ChatAgent + ResearchAgent 支持 context 注入
  Step 4: agents/orchestrator_agent.py — 编排中枢
  Step 5: E2E 联调

埋雷清单在 §六，目前选择"先开发再回头收"。
历史开发记录在 docs/archive/phase1-phase2-history.md。
设计讨论过程在 NOTES.md。
```

---

## 八、安全提示

- 项目已是 git 仓库（2026-05-23 `git init`），`.env` 含真实 API Key，已通过 `.gitignore` 排除（连同 `__pycache__/`、`reports/`、`.DS_Store`）
- `.env.example` 是安全模板，已入库
- 提交前务必确认 `.env` 不在 `git diff --cached --name-only` 列表中
