# 项目评估报告 — agentry-shell（iter-01）

> 评估者：独立 Evaluator（未参与开发）。日期：2026-05-29。
> 方法：以"实际运行 + file:line 证据"为准，不默认信任历史 markdown 结论。
> 运行基线：跑通 echo 管线、一次真实 LLM 的 zero-dispatch 对话（`deepseek-v4-pro`，4.6s）、
> 12 个 `tests/check_*.py` 全绿；现场触发失败路径（arxiv 503）。
> **未能核实**：完整复合 happy path（research→stage→wiki）两次均未在 ~17 min / ~5 min 内完成（详见 P0）；
> wiki 去重/复用行为因此未现场验证。

---

## 1. 一句话总体结论

**工程质量与代码自洽度高、设计叙事清晰，hub-and-spoke 的上下文隔离是真的成立的；但"当前活跃路径（v2 Coordinator）"与"README 重点宣传的能力（v1 意图路由 / 连续对话）"已经分叉，且 happy path 在真实环境下受 arxiv 503 + 无 LLM 超时拖垮——属于"代码可读性优秀、单元闭环扎实，但端到端鲁棒性与文档一致性未到 review-grade"的成熟度。**

---

## 2. 发现清单（按严重度）

### [P0] 复合 happy path 在真实环境不可靠完成 — arxiv 503 + 无 per-call 超时叠加
- **证据**：现场两次跑 `python cli.py "...调研...归档进wiki"`。窄化版实时日志显示 Coordinator 第1轮（7.8s）正确分解 → `dispatch_agent(researcher)` → researcher 第1步发 `search_papers` → `✗ Error: ... HTTP 503 (export.arxiv.org/...max_results=100)`，随后下一轮 LLM 调用 **>2.5 min 无产出**；首次宽任务跑满 ~17 min（CPU 仅 4.5s，纯 I/O 阻塞）无任何输出。`core/retrievers/arxiv.py:36` 的自研重试 **只匹配 `"429"`**，503 在首次即 re-raise；LLM provider 未设 per-call timeout（`AsyncOpenAI` 默认 600s），慢/挂的调用会阻塞整轮。
- **验收标准**：(a) 重试覆盖 503/连接错误，带退避；(b) 给每次 LLM/检索调用设可配置超时（如 60s），超时转 observation 而非挂起；(c) 提供一个不依赖外网的 demo 模式（local-file retriever 或缓存 fixture），使 README 首条命令在 <60s 内稳定出结果。

### [P1] 活跃 hub（Coordinator）无跨轮记忆，但 README 把"连续对话/会话记忆/指代消解"作为头条能力
- **证据**：`agents/coordinator_agent.py:99-102` 每次 `run` 重建 `messages=[system,user(task)]`，**从不 import `SessionManager`**；`SessionManager` 只被 v1 `orchestrator_agent.py:42` 使用。README `## Highlights` 的 "Intent-driven orchestration … multi-turn conversation … Pronoun resolution and follow-ups work" 与 Quick start 的 "Multi-turn conversation (session memory across turns)" 描述的是 dormant 的 v1。`--interactive` 下用 Coordinator 多轮无记忆。
- **验收标准**：README 明确区分"v2 活跃路径的能力边界"（单任务内分解，无跨轮记忆）与"v1 历史能力"；或把 session 记忆接回 Coordinator。运行 `--interactive` 连发两轮带指代的追问，行为与文档一致。

### [P1] 跨 agent observation 回灌完整报告，与项目主打的 −72% 教训自相矛盾（复合任务 token/context 膨胀）
- **证据**：`core/dispatch.py:135-148` `_format` 把 spoke 的完整 markdown 报告以 `---\nreport:\n<全文>` 塞回 hub 的 observation，`coordinator_agent.py:199` 再 append 进 hub messages。README 三-spoke 示例（RL+Agent+vLLM 并行→归档）会把 3 份完整报告累积进 hub 上下文。这与 README "Optimizing the ReAct loop, −72%" 的核心结论（把全文移出循环）方向相反。
- **验收标准**：hub 默认只消费 `summary`/`artifact_path`；`report` 全文按需（仅当下游确需且无 artifact 时）注入，或截断。给出复合任务 hub 累计 token 的前后对比。

### [P1] staging/ 暂存区泄漏进策展产物 index.md
- **证据**：`core/wiki_index.py:80` `collect_pages` 用 `rglob("*.md")` 仅跳过 `index.md`，未排除 `staging/`。现场 `wiki/index.md` 已含垃圾条目：`## 未分类 → [agent-evaluation-survey](staging/agent-evaluation-survey.md) —`（空描述，无 frontmatter 的原料）。`ListFilesTool` 同样会列出 staging。
- **验收标准**：`collect_pages`/`list_files` 排除 `staging/`（及 `index.md`）；重生 index 后不再出现 staging 条目。加一条断言到 `check_wiki_index`。

### [P1] registry 的 input/output_contract 是手写散文，已二次漂移（曾是上次大 bug 的根因）
- **证据**：`core/registry.py:88` `wiki_curator.input_contract` 仍写「prompt = 归档指令，含要处理的 .md 文件路径（如「把 reports/foo.md 归档进 wiki」）」，但实际契约是"必须先 `stage_files`、wiki_curator 只能读 `staging/`"（见 `coordinator_agent.py:58-67` 的 SCHEMA）。两处对同一下游给出冲突指引。`case-facts.md §2/§12` 记载上一次正是 `output_contract` 说谎导致 LLM 猜路径。
- **验收标准**：registry 契约与 SCHEMA 单一来源、互不矛盾；或让契约从代码 schema 派生（case-facts §12 已提此方向）。

### [P1] v1 wiki 归档路径已被 staging 重构悄悄打断（且 README `.env` 默认指向 v1）
- **证据**：Step-1 后 `ReadSourceTool` 只允许 `staging/` 前缀（`core/tools.py:176`），而 v1 `orchestrator_agent.py:82-85` 路由 wiki 时**从不 stage**，只 `worker.run(task, files=intent.files)`（`files=` 被 WikiAgent 忽略）。故 v1 的 `route=wiki` 现在无法喂给 WikiAgent。`case-facts.md §12` 仍称其"碰巧仍走通"——该结论已过期。同时 README Quick start 的 `.env` 块写 `AGENT_CLASS=...OrchestratorAgent`（v1）。
- **验收标准**：要么删/隐藏 v1 wiki 路由，要么补 stage；README/.env 统一默认到能跑通的 hub。

### [P2] 测试无法"按 README 一条命令"跑：pytest 未装且脚本不可被 pytest 收集
- **证据**：`python -m pytest` → `No module named pytest`；`tests/check_*.py` 是 `__main__`+`assert` 脚本，无 `test_` 函数，`pyproject.toml` 虽配了 `[tool.pytest.ini_options]` 但收集为空。README "Status" 自承"folding … into a one-command pytest suite"为 in-progress。12 个脚本我逐个 `python tests/check_*.py` 全绿。
- **验收标准**：`pip install -e ".[dev]" && pytest` 一条命令绿；或 README 明确给出 `for f in tests/check_*.py; do python $f; done` 的真实跑法。

### [P2] 三处 AGENT_CLASS 默认值互相打架，增加从零复现摩擦
- **证据**：README `.env` 块=`OrchestratorAgent`；`.env.example`=`EchoAgent`；实际 `.env`=`CoordinatorAgent`；`core/config.py:42` 默认=`EchoAgent`。RETRIEVER 同样：README=`arxiv`，`config.py:50` 默认=`arxiv,tavily`。
- **验收标准**：`.env.example` 与 README Quick start 给出同一套"能跑通 v2"的值。

### [P2] 死代码 / WS token 计量在多 spoke 下失真
- **证据**：`core/stream.py:14` `stream_output()` 无人调用；`backend/server/websocket_manager.py:60` 每连接分配 `asyncio.Queue` 但从不消费（事件经 `_send_event` 直发）。另：`websocket_manager.py:140-148` 用 `scope=="cumulative"` 直接覆盖 `task_cumulative`，多个 spoke（ResearchAgent 发 `scope=cumulative`）会互相覆盖、且 hub 自身（无 scope）被忽略 → 复合任务会话级 token 统计只剩最后一个 spoke。
- **验收标准**：删死代码；WS 计量改为累加所有 tokens 事件（或由 hub 汇总后单发一条权威 cumulative）。

### [P2] degenerate 检索仍兜底落盘"未找到"报告
- **证据**：`agents/research_agent.py:179` 当 `artifact_path is None and final_content.strip()` 即落盘，即使 `status=degenerate`（`case-facts.md §7.6` 已记此瑕疵）。
- **验收标准**：degenerate 时不落盘、不产 artifact，observation 明示无结果。

---

## 3. 存档分流三栏表

| 条目（出处） | 分类 | 证据 / 说明 |
|---|---|---|
| ResearchAgent 不落盘 / WikiAgent 只吃路径 / output_contract 说谎（case-facts §2） | **已解决** | staging 工作流已建：`SaveReportTool`（research_tools.py:273）+ `stage_files`（staging.py）+ `read_source` 限 staging。复合任务首段已能正确 dispatch+落盘（现场观测到 researcher 落盘意图） |
| BaseLLM 无 tool calling（wiki-agent开发.md:151） | **已解决** | `openai_provider.py:17/64` 已实现工具序列化+解析，`check_tool_calling` 绿 |
| 路由 if/elif 硬编码（Orchestrator优化 Tier2 #3） | **已解决** | v2 改为 registry 驱动（`registry.py` + `dispatch.py`），活跃路径不再硬编码 |
| `agents/__init__` 自动 import 会炸（§七 P1 埋雷） | **基本已解决** | 现仅 import EchoAgent（无三方依赖），`import agents` 不再拉全部 agent；原"导入全部"隐患不成立 |
| B4 前端多轮追加（§七） | **已解决（未现场复验）** | 文档标 ✅；本次未跑 Web UI |
| `session_id=id(websocket)` 脆弱 + 无 eviction（Orchestrator优化 Tier1 #1） | **仍然有效（v1 dormant）** | `orchestrator_agent.py:46-47` 原样；v1 非活跃，影响范围收窄 |
| 同步文件 IO 阻塞 event loop（Tier1 #2） | **仍然有效（v1）** | `session.py:102` `write_text` 在 async 路径；仅 v1 用 |
| `POST /api/run` 同步阻塞（§七 P1） | **仍然有效** | `app.py:97-119` 仍同步收集全事件；README 已披露为 in-progress |
| `core/stream.py` 死代码 / WS `active` 队列无用（§七 P2） | **仍然有效** | grep 确认 `stream_output` 无调用、`active` 队列从不消费 |
| registry `wiki_curator.input_contract` 过时（case-facts §12） | **仍然有效** | `registry.py:88` 仍是 path-based，与 SCHEMA 冲突（见 P1） |
| `intent.files` 沦为废纸（case-facts §12） | **仍然有效（v1）** | intent.py 仍填充，WikiAgent 忽略 |
| degenerate 仍兜底落盘（case-facts §7.6） | **仍然有效** | `research_agent.py:179` 确认 |
| Anthropic 工具路径未实现（CONTEXT:45） | **仍然有效** | `anthropic_provider.py:40-41` `tools` 非空即 `NotImplementedError`（README 以"text path"披露） |
| 冷启动首轮 intent 降级 research（B9/B10） | **已过期/不适用 v2** | v2 Coordinator 无 intent 分类器；仅 v1 dormant 路径仍真 |
| v1 wiki 路由"碰巧仍走通"（case-facts §4.5/§12） | **已过期（且更糟）** | staging 沙箱化后 v1 不 stage→WikiAgent 读不到；该结论已 stale，实为 v1 隐性 bug（见 P1） |
| orchestrator description 用正文快照（B8） | **已过期/不适用 v2** | v2 不走 SessionManager；v1 only |

> 注：存档里的 TODO/埋雷未直接列入第 2 节"现存问题"，已按要求逐条核对当前代码后归类。

---

## 4. 针对"作品集故事"的 3 条最高杠杆改进

1. **统一叙事到 v2，并把"分叉"本身讲成卖点。** 现在 README 同时摆着 v1 架构图+意图路由头条 与 v2 Coordinator，面试官读到的重点能力恰是 dormant 的那个。改法：架构图换成 v2 hub-and-spoke；把 v1→v2 的演进（为何放弃固定意图路由、改为 ReAct 涌现式分解）写成一小节"设计取舍"——这比假装一直是 v2 更能展示工程判断，正是这类项目最该展示的东西。

2. **给一个 60 秒、不靠外网就能跑的可复现 demo。** 当前 happy path 受 arxiv 503 + 无超时拖死，面试官现场跑大概率挂（P0）。做一个 `RETRIEVER=local` 或带缓存 fixture 的 demo 任务 + 一段 asciinema/GIF 录屏放进 README，并让 `.env.example` 默认即可跑通 v2。这一条同时消化 P0/P2-config 两个问题，是"5 分钟看懂"的关键。

3. **让知识库形成闭环（写入→可被复用）。** 现在 wiki 只写不读：`ChatAgent` 不查 wiki（README roadmap 自承），report 进库后无人复用，"知识复利"的故事缺了后半句。哪怕加一个 `wiki_search`/`read_file` 让 Coordinator 在 chat 路径能引用已归档页面，就能把"research→curate→reuse"讲成完整回路——这是 multi-agent 知识系统最有说服力的一幕。顺带先修 staging 泄漏（P1），否则 reviewer 打开 `wiki/index.md` 第一眼就是一条空描述的垃圾条目。
