# Agentry-Shell

以理解、学习Agent核心思路而写的**可扩展 LLM Agent 运行框架**。不是对某个框架的封装——从 Agent 生命周期、事件协议、hub-and-spoke 编排、ReAct 工具循环、流式传输，到可插拔的 LLM Provider 与检索器，每一层都在这里自己实现、自己拥有,目的就是搞清楚一套真实的 Agent 系统到底是怎么拼起来的。

名字里的 `-shell`（外壳）是刻意的：这个项目是一个**运行 Agent 的底座**，而不是某一个 Agent。换一个 Agent 类、一个 LLM Provider、一个检索器，基础设施层不用改。

架构最初是研究 [gpt-researcher](https://github.com/assafelovic/gpt-researcher) 的产物——它的「检索器 / Agent」分层是最早的参考——之后长出了自己的 Agent 运行时、事件协议、编排中枢和 ReAct 工具循环。没有任何一层是当黑盒引进来的。编排层本身还经历过一次重构（v1 固定意图路由 → v2 涌现式 ReAct 分解），见下文 [设计演进](#设计演进v1--v2)。

```
用户（CLI / Web UI / REST）
        │
        ▼
   run_agent()                       ← 统一的生命周期 + status/error 包装（core/runner.py）
        │                              Agent 只管 yield 领域事件、失败直接抛异常
        ▼
  CoordinatorAgent（中枢/hub）        ← 一个 ReAct 循环，工具是 dispatch_agent / import_files / wiki_search / read_file
        │  临场把任务分解，派发给 spoke（按依赖决定并行或串行），
        │  只把每个 spoke 的「摘要 + artifact 路径」收回中枢（而非完整报告全文）
        ├─ dispatch_agent(researcher)   → ResearchAgent  ← 内部 ReAct 循环：search_papers / search_web /
        │                                                  fetch_url / do_broad_survey → save_report 落盘报告
        └─ dispatch_agent(wiki_curator) → WikiAgent      ← 内部 ReAct 循环：把 wiki/staging/ 的原料策展进持久化的 ./wiki/
```

中枢从不把 spoke 的完整 transcript 或报告全文塞进自己的上下文，而是在 Agent 之间传递**摘要和 artifact 路径**，让下游 spoke 自己去读文件。这条「上下文纪律」正是 wiki 循环 [−72% token](#优化-react-循环可度量) 背后的同一根杠杆。

## Agent 一览

通过 `AGENT_CLASS` 指定中枢（默认 `CoordinatorAgent`）；spoke 由中枢派发或直接调用。

**中枢（启用）**
- `CoordinatorAgent`（v2，**默认**）——它本身就是一个 ReAct 循环，工具是 `dispatch_agent`、`import_files`、`wiki_search`、`read_file`。临场把请求分解，按依赖并行或串行地派发 spoke，最后综合出面向用户的回答。
  - **健康度仪表盘**：每轮把「原始目标 + 轮次预算 + 派发台账与止损 + 进度自检」拼在请求末尾，且**不写进 messages**——这是刻意的 KV-cache 优化（变化内容放末尾，整段 prompt 才不会每轮 cache miss）。台账记录每个 spoke 的成/败/连续失败次数，连续失败 ≥2 次就亮「止损」提示。
  - **触顶诚实收尾**：撞到 `MAX_ROUNDS=10` 时强制一次无工具调用，让模型据实交代「做到哪 / 缺什么 / 为什么缺 / 试过什么 / 下一步」，而不是编占位话术。

**中枢（休眠）**
- `OrchestratorAgent`（v1，**休眠**）——每轮一次 fast-LLM 意图分类 → 固定路由到单个 worker，带跨轮会话记忆。保留作对照，已非默认，其 `wiki` 路由已废弃（见 [设计演进](#设计演进v1--v2)）。它是 `core/session.py` 跨轮记忆的唯一使用方。

**spoke（辐条）**
- `ResearchAgent`——自己的 ReAct 循环，工具有 `search_papers`（arXiv）、`search_web`（Tavily）、`fetch_url`（抓网页去 HTML）、`do_broad_survey`（拆问 → 多源并发 → 综合）、`save_report`。LLM 看 prompt 自决用哪个工具，没有 `mode` 分发。三态 status：检索全空 → `degenerate`；触顶未完成 → `incomplete`；正常 → `ok`。调 `save_report` 即声明「这是成品」并落盘 `reports/`，让中枢把路径传给下游；漏调时代码兜底落盘，但 degenerate / 触顶不兜底（避免把空壳泄漏进 wiki）。
- `WikiAgent`（派发名 `wiki_curator`）——ReAct 循环，把搬进 `wiki/staging/` 的 `.md` 原料策展进持久化的 `./wiki/` 知识库。文件工具跑在路径沙箱里（限定 `./wiki/`，挡住穿越 / 绝对路径 / 符号链接逃逸）；确定性的 `index.md` 由**代码**从各页 frontmatter 重生成，**LLM 永不读写它**，`staging/` 不进策展索引。
- `ChatAgent`——单轮问答；在 v2 下塌缩成 Coordinator 的「零派发」路径。
- `EchoAgent`——零 LLM 的参照 Agent，用来验证 CLI / WebSocket 管道。

### 示例：一个复合任务的端到端

> *「并行调研 RL 和 Agent 框架的近期进展，然后分析 vLLM 仓库，最后把这些全部归档进 wiki。」*

在 `CoordinatorAgent` 下，这会变成嵌套的 ReAct 循环：

1. **第 1 轮**——三个无依赖子任务，一轮里并发派出：
   - `dispatch_agent(researcher, "调研 RL 近期进展 …")` → `ResearchAgent` 跑 `do_broad_survey`。
   - `dispatch_agent(researcher, "调研 Agent 框架近期进展 …")` → 第二个实例并行。
   - `dispatch_agent(researcher, "分析 vLLM 仓库 …")` → 第三个用 `search_web` + `fetch_url`。
2. **第 2 轮**——依赖第 1 轮：中枢把三份报告 artifact 交给 `dispatch_agent(wiki_curator, "归档 staging/ …", context=…)`（派发前的 `stage_wiki_inputs` pre-hook 自动把报告搬进 `wiki/staging/`）→ `WikiAgent` 写 / 更新 `./wiki/` 页面；代码重生成 `index.md`。
3. **第 3 轮**——无工具调用，Coordinator 输出最终 markdown 综合。`spokes_used` 走事件 metadata，不进正文。

单 spoke 调用也成立——中枢只是分解成一次派发：`python cli.py "调研 speculative decoding"`。

## 设计演进：v1 → v2

编排层被重写过一次，这个对照是项目里最有意思的部分。

**v1——固定意图路由（`OrchestratorAgent`，已休眠）。** 每轮跑一次 fast-LLM 分类，拆进正交的轴（`route` / `mode` / `target` / `carry_context` / `files`），路由到恰好一个 worker，`SessionManager` 带着历史报告做跨轮追问。它能跑，但路由表是瓶颈：每加一个能力就要加一条 `route` 和新的分类样例，复合请求（「调研 A **和** B，再把两者归档」）塞不进单一路由，固定的 `mode` 枚举又预先替 worker 决定了「怎么做」。

**v2——涌现式 ReAct 分解（`CoordinatorAgent`，默认）。** 中枢本身是一个 ReAct 循环，唯一的路由原语是 `dispatch_agent`。没有意图表：LLM 读注册表 catalog，自己决定**派给谁**、**给什么自包含的 prompt**、**按什么顺序**——子任务独立时一轮发多个派发，下游 prompt 需要上游 artifact 时则串行。spoke 也变 agentic 了（`ResearchAgent` 自己选工具，不再听 `mode`）。中枢↔spoke 契约刻意收窄：spoke 只回「一行摘要 + artifact 路径」，中枢把它们往下传，而不是把完整报告再内联——在长复合任务里保持中枢上下文很小。

**能力边界（判断范围前先读这条）。**
- **v2 在单个任务内分解**——它**没有跨轮记忆**。每次 `cli.py` 调用（以及 `--interactive` 的每一行）都是独立的，Coordinator 不记得上一轮。
- **跨轮会话记忆是 v1 的能力**（`SessionManager`），而 v1 休眠。把会话记忆接回 v2 中枢明确是*未来工作*，不是现有特性。

## 亮点

- **Hub-and-spoke 的 ReAct 编排。** `CoordinatorAgent` 靠*推理*分解请求，而非路由表：派发 spoke（并行或串行），只在它们之间传摘要 + artifact 路径，再综合结果。没有固定意图枚举，没有逐能力的路由代码。
- **Agent 运行时 / harness。** 单一 `AgentInterface` 契约 + `run_agent()` 驱动包揽整个生命周期（`on_start` → `running` → `done` / `error`）。Agent 保持简单：实现一个异步生成器、`yield` 领域事件、失败直接抛。status / error 的发射永远不是 Agent 的活。
- **统一事件协议。** 每个 Agent 都说 `AgentEvent`（`log` / `stream` / `result` / `status` / `tokens` / `error` / `custom`）。同一条事件流驱动 CLI、WebSocket 和 Web UI——传输层与 Agent 解耦。
- **带护栏的真 ReAct Agent。** `ResearchAgent` 和 `WikiAgent` 都是货真价实的工具调用循环。工具跑在沙箱注册表里（wiki 工具限定 `./wiki/`，挡穿越 / 绝对路径 / 符号链接逃逸），工具永不抛异常（失败变 observation，循环不被工具打断），循环带断路器（重复调用 nudge、`MAX_STEPS` 上限）。检索 / LLM 调用有 per-call 超时和瞬时错误退避重试，把失败变 observation 而非把这一轮挂住。
- **声明式文件沙箱（`core/scope.py`）。** `Scope` 是一个 frozen dataclass：根目录 + 策略（可写 / 子目录 / 后缀白名单 / 禁止名 / 大小上限）。`resolve()` 是唯一校验入口，`.resolve()` 摊平 `..` 与符号链接防穿越。过去每个文件工具各自手写路径安全、散落易漏；现在策略集中声明一次，新增 Agent = 写一行 `Scope(...)` 复用同一批工具。
- **到处可插拔。** LLM Provider（OpenAI / DeepSeek；Anthropic 文本路径）在 `factory` 后面、带 `smart` / `fast` 两档；检索器（arXiv / Tavily / 本地文件）在 `BaseRetriever` 后面；Agent 经 `AGENT_CLASS` 动态加载。加任何一种都不碰核心代码。
- **流式优先。** WebSocket 上的 token 级流式，带 per-session token 计量。

## 可观测性：无损追踪 + 复盘

ReAct 的真实过程过去只活在某次 `run()` 的 `messages` 数组里，函数返回即销毁。这一层给它一个独立于「显示」的「存档」出口——也是项目的一个亮点。

- **`core/trace.py`——无损追踪。** tap 点在 `BaseLLM.chat`（所有 provider 之下、所有 agent 之上），新 agent / provider 自动被记、零改动。每次 chat 原样落「本次完整 input 快照 + 完整 output」，**完全不理解 input 内容**（与功能层彻底解耦：中枢末尾搭车的仪表盘随便变，日志层一行不改）。每条记录带 `run_id / parent_run_id / agent / conv_id`——因为 spoke 跑在 `asyncio.gather` 复制的隔离 context 里，hub→spoke 父子树天然成立、并发兄弟互不串味。落盘 JSONL（`traces/run-<ts>-<pid>.jsonl`），`TRACE=0` 可关。
- **`scripts/replay.py`——读端复盘。** 把 trace JSONL 还原成人读的 transcript：`build_tree()` 先建结构化节点树（已做 diff / 窗口），终端渲染器和 HTML 渲染器消费同一份模型——保证两边展示同一内容、易错的 diff / 窗口逻辑只有一份不漂移。能完整还原此前永远拿不回的三样：每个 agent 的完整 system prompt、LLM 原样输出的 tool_call 全参数（含整篇报告）、LLM 实际收到的工具结果原文。支持 `--html` / `--open` / `--max` / `--window`。

这条「写读分离」是贯穿全代码的原则之一：易错的 diff / 窗口逻辑放在可重算的读路径，写路径只管最简最稳地如实落盘。

## 优化 ReAct 循环，可度量

`WikiAgent` 的工具循环用「先建尺子再优化」的流程做了 profile 和调优：先有事件级追踪（每步推理、工具调用、计时、token 增量），再固定一个单主题的归档 fixture 让多次运行可比较。在该 fixture 上把一篇文档归档进一个两页的 wiki：

| 版本 | LLM 往返 | 工具调用 | 总 token |
|---|---|---|---|
| 基线 | 6 | 读 index · list · **读 2 个无关页** · 写页 · 重读 index · 写 index | 28,264 |
| + 仅凭 index 判相关性、只为更新才读页 | 4 | 读 index · list · 写页 · 写 index | 14,435 |
| + 索引交给代码（LLM 永不读写它） | **2** | 写页 | **7,943** |

**−72% token，策展产物完全一致。** 两个发现塑造了这项工作：

- *先把仪表做出来。* 在一篇含歧义、多主题的文档上，单次 token 数被策展方差主导（模型这次建 1 页、下次建 3 页），淹没了信号。一个固定单主题 fixture + git 跟踪的基线 wiki + 一行命令重置，才让每次改动的效果可读。
- *一个被证伪的假设重定向了努力。* 「裁剪模型累积的 `reasoning_content`」看着像最大的杠杆——但 thinking 模式的 API 会拒绝任何丢掉它的工具调用轮，推理无法从历史里删。于是努力转向删掉*积累它的那些轮*：凭 index 判相关性而非读页、把索引维护推出循环交给确定性代码。

同一条「传摘要 + artifact 路径、而非完整 transcript」的原则，正是 v2 中枢在多 spoke 任务里保持上下文小的原因。

## 快速开始

需要 Python ≥ 3.11。

```bash
git clone git@github.com:QingQingS/agentry-shell.git
cd agentry-shell

python -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env      # 然后填入你的 LLM API Key
```

最小 `.env`（示例用 DeepSeek；OpenAI 同理）：

```bash
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-...
SMART_LLM_MODEL=deepseek-chat
FAST_LLM_MODEL=deepseek-chat
AGENT_CLASS=agents.coordinator_agent.CoordinatorAgent   # v2 中枢（默认）
RETRIEVER=local                 # 离线 fixtures（默认）；或：arxiv / tavily / arxiv,tavily
```

### 60 秒离线演示

`RETRIEVER=local` 完全跑在缓存的本地语料（`fixtures/`）上——**不触任何外部检索服务**（无 arXiv / Tavily），所以演示在不稳的网络下也可复现。你仍需一个 LLM API Key（Agent 的推理循环要调模型）。

```bash
bash scripts/demo.sh
# → ResearchAgent 在 fixtures/ 上跑它的 ReAct 循环，把报告写进 reports/，检索全程离线。
```

> 📹 **演示录制占位** —— 60 秒演示的 asciinema / GIF 将放在这里。*（TODO：录制 `scripts/demo.sh` 并嵌入。）*

跑通用入口：

```bash
# 单次任务（默认中枢 = CoordinatorAgent；默认检索 = local）
python cli.py "调研 speculative decoding 的近期工作"

# 交互式 REPL —— 每行作为独立任务运行（v2 无跨轮记忆）
python cli.py --interactive

# Web 服务 → http://localhost:8000 （FastAPI + WebSocket + 极简 UI）
python main.py
```

### 复盘一次运行

任何一次运行默认都落了 trace（`TRACE=0` 可关）。复盘最近一次：

```bash
python scripts/replay.py                    # 终端 transcript
python scripts/replay.py --html --open      # 生成 HTML 阅读页并打开
```

### 跑检查

暂无 `pytest` 套件；`tests/` 下的验证脚本可逐个运行，全部离线（它们 stub 掉 LLM 和检索器，无需 API Key）：

```bash
for f in tests/check_*.py; do python "$f"; done
```

## 目录结构

```
core/
  agent_interface.py   # AgentInterface + AgentEvent —— 每个 Agent 实现的契约
  runner.py            # run_agent()：生命周期 + status/error，收敛在一处
  dispatch.py          # dispatch_agent 工具：隔离跑 spoke → 摘要 + artifact observation
  registry.py          # AgentRegistry：spoke catalog + 中枢派发用的 factory
  staging.py           # import_files（中枢工具）+ stage_wiki_inputs（wiki 派发前 pre-hook）
  scope.py             # Scope：声明式文件权限原语（路径沙箱）
  trace.py             # 无损追踪：BaseLLM.chat tap → input 快照 + 完整 output 落 JSONL
  session.py           # SessionManager：轮次 + 滚动报告窗口（v1；休眠）
  intent.py            # classify_intent（v1 路由；休眠）
  tools.py             # Tool / ToolRegistry + wiki 的沙箱文件工具 + WikiSearchTool
  wiki_index.py        # 从页面 frontmatter 确定性投影出 index.md
  config.py            # 配置解析：env > .env > 默认
  llm/                 # BaseLLM、OpenAI/DeepSeek/Anthropic providers、factory、工具调用路径
  retrievers/          # BaseRetriever + arXiv / Tavily / 本地文件源
agents/
  coordinator_agent.py   # v2 中枢：在 spoke 注册表上做涌现式 ReAct 分解
  research_agent.py      # spoke：内部 ReAct 循环，多源检索 → 报告 artifact
  research_tools.py      # ResearchAgent 的私有工具层
  wiki_agent.py          # spoke：ReAct 工具循环 —— 持久化知识策展
  chat_agent.py          # 上下文感知的单轮聊天
  orchestrator_agent.py  # v1 意图路由中枢（休眠）
  echo_agent.py          # 零 LLM 的参照 Agent
backend/server/          # FastAPI 路由 + WebSocket 管理
frontend/                # 极简 HTML / JS / CSS 客户端
fixtures/                # 离线 RETRIEVER=local 演示用的缓存语料
scripts/                 # demo.sh（离线演示）+ replay.py（trace 复盘）
cli.py  main.py          # CLI 与 Web 入口
```

## 扩展

**加一个 Agent** —— 实现一个方法，把 `AGENT_CLASS` 指过去，完事：

```python
from core.agent_interface import AgentInterface, AgentEvent

class MyAgent(AgentInterface):
    async def run(self, task: str, **kwargs):
        yield AgentEvent(type="log", content="working...")
        result = await do_something(task)     # 失败直接抛；run_agent() 兜
        yield AgentEvent(type="result", content=result)
```

```bash
AGENT_CLASS=agents.my_agent.MyAgent python cli.py "..."
```

**给 v2 中枢加一个 spoke** —— 在 `core/registry.py` 注册一条 `AgentSpec`（name、何时用的 description、输入 / 输出契约、factory）。Coordinator 自动从 catalog 拾取，没有路由代码要碰。新的 LLM Provider 和检索器同理，分别挂在 `core/llm/factory.py` 和 `BaseRetriever` 后面。

## 状态与路线图

作为学习项目持续开发中。Agent 运行时、v2 hub-and-spoke Coordinator、多源研究、ReAct WikiAgent，以及无损追踪 + 复盘，都已实现、能对接真实 LLM 端到端跑通；离线 `RETRIEVER=local` 路径让检索侧不触外网。Coordinator 已能回查策展好的 wiki（`wiki_search` + `read_file`），「研究 → 策展 → 复用」的闭环已经接上。

已知粗糙处（公开披露、尚未处理）：WebSocket 多 spoke 的 token 计量、同步的 `POST /api/run` 路径、Anthropic 工具路径（目前仅文本）、把 `tests/check_*.py` 收进一条 `pytest` 命令。v1 `OrchestratorAgent` 休眠。

下一步计划：把跨轮记忆接回 v2 中枢（把 `session.py` 接回并落 SQLite）；语义检索（嵌入 + 关键词混合，替掉当前的词袋打分）；以及一个评估闭环（固定任务集 + 跨 run 的成本 / 时延 / 成功率聚合，复用 trace 数据）。

## 技术栈

Python · asyncio · FastAPI · WebSocket · Pydantic · OpenAI / DeepSeek / Anthropic SDK · arXiv · Tavily

## 致谢

最初的框架是通过研究并复现 Assaf Elovic 的 [gpt-researcher](https://github.com/assafelovic/gpt-researcher) 搭起来的。它分层的检索器 / Agent 设计塑造了早期脚手架；这里的运行时、编排和 ReAct 策展层是为了搞懂每块怎么工作而做的独立重新实现。
