# prompt1

把我们这次讨论中所有已经确定的决定、约定、踩过的坑、未决问题，以条目形式保存到 case-facts.md。逐条列出，不要概括，不要省略具体的文件名、函数名、数值和被否决的方案
# prompt2
根因诊断：
先梳理当前有什么问题，用精炼的文字进行描述，让没有改项目背景的人也能理解这个agent系统存在的问题，先做好这件事

# case-facts.md

本次会话的逐条事实记录。覆盖：根因诊断、两轮设计调整、3 步实施细节、被否决方案、测试现状、未决问题。

---
## 这个 Agent 系统现在的问题（20260601）

  先给 30 秒背景

  这是一个「多智能体协作」系统，结构是一个总指挥 + 几个专才：

  - 总指挥：接到用户任务后，自己把任务拆成子任务，分派给下面的专才，最后把各路结果汇总成给用户的答复。
  - 专才：目前两类——「研究员」（检索资料、写报告）和「归档员」（把报告整理进本地知识库）。
  - 不论总指挥还是专才，内部都是同一种工作方式：想一步 → 做一个动作 → 看结果 → 再想下一步，循环往复，直到自己认为做完了。
  - 为防止无限循环，每个角色都设了一个硬性步数上限，到点就强制停。

  一句话概括病根

  系统里每个角色都只会处理「一切顺利」的情况，不会处理「中途出岔子」的情况。 一旦出问题，它既察觉不到、也没有应对办法，只会一路撞到步数上限被强行掐断。

  具体暴露的问题

  拿一个真实任务举例：「检索具身智能里 agent 的最新进展，并归档」。实际跑下来：

  1. 不会判断「任务做得好不好」，于是反复重试。
  研究员因为检索源是离线的本地文件、搜不到真实资料，交回的报告质量很差。总指挥对结果不满意，但它唯一会的反应就是「再派一次研究员」——它分不清「这次没搜到」是
  「换个问法还能救」还是「这条路根本走不通」。结果就是对同一类任务反复重试，白白烧掉预算。

  2. 对「自己还剩多少预算」毫无概念。
  总指挥清楚「后面还有归档这一步」，却仍不停地派研究员。因为它根本不知道自己总共能做几步、已经用了多少、该不该给归档留余地。等步数耗尽，归档这步永远没机会执
  行——用户明确要求的「并归档」直接落空了。

  3. 任务越拆越偏，离主线越来越远。
  因为「宽口径调研」那条路走不通，总指挥只能把大问题拆成越来越小的问题。更糟的是，每个小问题都是「对上一个结果不满意」派生出来的，而没有任何一步回头确认「这
  还在原任务的主线上吗」。于是主题从「具身智能最新进展」一路漂移到「某创业公司的某款机器人单品」，最后甚至在查「某公司创始人叫什么名字」——完全偏离了用户初衷
  。

  4. 出了问题不会如实告诉用户。
  当总指挥被步数上限强行掐断时，它只丢回一句占位话术（「结果可能不完整」），不会讲清到底做到了哪、缺了什么、为什么缺。用户拿到一个看不出问题出在哪的半成品。

  共同根源

  这四条不是四个独立的 bug，而是同一个缺口的四个切面：系统从设计上就没有「任务健康度」这个概念——

  - 没有人评估「做得够不够好」（→ 问题 1）
  - 没有人盯着「预算够不够用」（→ 问题 2）
  - 没有人管「有没有跑偏」（→ 问题 3）
  - 没有人负责「出了问题如实上报」（→ 问题 4）

  每个角色都默认「任务会顺利完成」，于是整条链路在异常面前既没有刹车，也没有方向盘。
## 落盘
raw 的 ReAct 过程从一开始就没被完整保留过。

  一、有没有落盘?——程序零落盘

  - core/runner.py:只把事件 yield 到内存,不写任何文件。
  - 全代码库没有任何 FileHandler / open(...log...) / 事件序列化。那几处 getLogger(arxiv.py / app.py / websocket_manager.py)是运维日志(连接数、报错),不是
  ReAct transcript。
  - CLI(cli.py)只 print 到 stdout;WebSocket(websocket_manager.py:171)只 send_json 推给前端。两条入口都不落盘。
  - 所以 trace.log 是你手动把终端输出重定向/复制出来的,内容就是 CLI 那套已经裁过的渲染——临时、有损,程序自己不产这个文件。

  二、就算抓下来,事件本身丢了什么?

  每个 agent 的循环只 yield 三类粗粒度日志,且都是摘要,不是原始数据:

  ┌─────────────────────┬─────────────────────────────────────────────────────────────┬─────────────────────────────────────────────────────────────────┐
  │ ReAct 里实际发生的  │                        事件里保留的                         │                            落了什么                             │
  ├─────────────────────┼─────────────────────────────────────────────────────────────┼─────────────────────────────────────────────────────────────────┤
  │ LLM 的完整原始输出  │ think 只取 reasoning_content 或 content                     │ 有 reasoning 时,真正的回答正文 content 被丢弃                   │
  │                     │ 之一(research_agent.py:141、coordinator_agent.py:131)       │                                                                 │
  ├─────────────────────┼─────────────────────────────────────────────────────────────┼─────────────────────────────────────────────────────────────────┤
  │ tool_call           │                                                             │ save_report 只记 filename、不记                                 │
  │ 的完整参数          │ _describe_action 只渲染一个字段(research_agent.py:269-279)  │ content(整篇报告);dispatch_agent 只记 agent 名、不记            │
  │                     │                                                             │ prompt/context/files                                            │
  ├─────────────────────┼─────────────────────────────────────────────────────────────┼─────────────────────────────────────────────────────────────────┤
  │                     │                                                             │ 检索到的实际内容全无;hub 侧更狠——observation 只记               │
  │ 工具返回的完整结果  │ _summarize_obs 只给条数/字数(:282-296)                      │ obs.splitlines()[0](coordinator_agent.py:213),即 [agent]        │
  │                     │                                                             │ status=ok 一行,summary/artifact/report 全丢                     │
  ├─────────────────────┼─────────────────────────────────────────────────────────────┼─────────────────────────────────────────────────────────────────┤
  │ 每个 agent 跑的     │ 完全不记录                                                  │ 无                                                              │
  │ system prompt       │                                                             │                                                                 │
  ├─────────────────────┼─────────────────────────────────────────────────────────────┼─────────────────────────────────────────────────────────────────┤
  │ 完整的 messages     │ 完全不记录                                                  │ 无(它只活在那次 run() 的内存里,函数返回即销毁)                  │
  │ 对话数组            │                                                             │                                                                 │
  └─────────────────────┴─────────────────────────────────────────────────────────────┴─────────────────────────────────────────────────────────────────┘

  三、结论:能复盘什么 / 不能复盘什么

  能看到(且仅在终端当下):每步的思考片段、调了哪个工具(名字)、大致拿到多少字、token 用量。

  永远无法复盘:
  1. 每个 agent 到底是带着什么 system prompt + 完整对话上下文 去问 LLM 的;
  2. LLM 原封不动吐了什么(尤其 tool_call 的完整参数——hub 发给 spoke 的真实 prompt、researcher 存进 save_report 的整篇报告);
  3. LLM 实际收到的工具结果原文(检索到的真实条目、完整 observation)。

  也就是说,你想回答的「这个 agent 的 ReAct 里 LLM 输出了什么、又接收了什么」——当前系统结构上就答不了,因为这些数据既没落盘,流过的事件也已经是压缩过的摘要。
## 1. 触发本次会话的 bug 现场

- 用户跑 CoordinatorAgent 复合任务（RL + Agent + vLLM 调研 + 归档）时，第 2 轮派 `wiki_curator` 一直返回 `没有可读的输入 .md 文档`。
- LLM 自言自语「实际中，researcher agent 可能把报告保存在了类似 `reports/rl_latest.md`」—— 这是它在猜路径。
- 最后 LLM 放弃归档步骤，直接综合输出给用户。

## 2. 根因（三因合一）

- **ResearchAgent 根本不落盘**：grep `agents/research_agent.py` 全文找不到 `write_text` / `open(` / `Path(...md)` 等写操作。它只 `yield AgentEvent(type="result", content=final_content, metadata={"status", "summary"})`，metadata 里**没有** `artifact_path` 字段。
- **WikiAgent 只吃路径**：`_resolve_input_paths` 把 `task` 按空白切分当 `Path()`；`wiki_agent.py:54` 找不到文件就 `raise ValueError("没有可读的输入 .md 文档")`，没有「内联内容」入口。
- **`core/registry.py:77` 的 `output_contract` 说谎**：写「一份研究报告（artifact 落盘），附一句话结论摘要」——这段会被 `AgentRegistry.catalog()` 渲染进 CoordinatorAgent system prompt，LLM 据此以为有 artifact，去找不存在的路径。
- 旁证：`core/session.py:91 save_report()` 才会真落盘，被 `orchestrator_agent.py:101`（v1）调用；v2 `CoordinatorAgent → dispatch → ResearchAgent` 完全绕开了 SessionManager。
- 另一条隐藏 bug：`dispatch.py:135-148` 已经把 spoke 的完整 report 塞进 observation `---\nreport:\n<markdown>`，hub 自己上下文里其实握着内容，但 WikiAgent 拒收内联文本，无法消费。

## 3. README 增补（前置的小改动）

- 在 `README.md` 标题段+架构图之后、`## Highlights` 之前插入一节 **"Agents at a glance"**。
- 语言：**英文**（与现有 README 一致），被否决：纯中文 / 中英双语。
- 位置：**标题段之后、Highlights 之前**，被否决：替换现有开头 / 作为 README 第一段（顶在标题正下方）。
- 内容：Hubs（CoordinatorAgent v2 active / OrchestratorAgent v1）+ Spokes（ResearchAgent / WikiAgent / ChatAgent / EchoAgent）+ 一个 vLLM+RL+Agent 复合任务的 Round 1/2/3 示例。
- 文字调和：明确写「Replaces the v1 intent-classified routing shown in the diagram above」，因为 ASCII 架构图仍是 v1。

## 4. 第一轮修复（WikiAgent 入口对齐 + 临时 read_source）

### 4.1 已确定

- WikiAgent.run 入口契约改为 `(task: str, **kwargs)` 与 ResearchAgent/ChatAgent 对齐；`kwargs` 识别 `context: str`。
- 删除 `_resolve_input_paths`、删除 `raise ValueError("没有可读的输入 .md 文档")`。
- `_format_input(today, catalog, context, task)` 拼接（date + catalog + 上游背景 + 归档指令）。
- 新增 `ReadSourceTool`（**第一版**：`Tool` 子类，**非**沙箱），让 LLM 看到 prompt 里的路径自决去读：
  - `ALLOWED_SUFFIXES = {".md", ".markdown", ".txt", ".rst"}`
  - `MAX_BYTES = 1024 * 1024`（1 MiB）
  - `path` 任意（相对或绝对，`Path(path).expanduser().resolve()`）
- `wiki_schema.py` 加 read_source 工具说明 + 工作流程 step 0「原料来源判断」。
- v1 OrchestratorAgent **不动**：`worker.run(task, files=intent.files)` 中的 `files=` kwarg 被 WikiAgent 默默吞掉；prompt 本身含路径文字，LLM 用 read_source 自决读，碰巧仍走通；`intent.files` 字段沦为废纸，等 v1 cutover 一并清。

### 4.2 测试改动

- `tests/check_wiki_agent.py`：
  - `scenario_natural` / `scenario_index_blocked` / `scenario_nudge_and_maxsteps`：把 `files=[str(src)]` 改成自然语言 `task=...`。
  - 删除 `scenario_no_input`（新接口不 raise）。
  - 新增 `scenario_read_source` 和 `scenario_read_source_rejects`。
  - 小 bug 修：`tempfile.TemporaryDirectory()` 返回 str，要 `tmp_dir = Path(tmp)`，否则 `tmp / "..."` 报 `TypeError: unsupported operand type(s) for /: 'str' and 'str'`。
- `tests/check_tools.py`：`specs()` 数量断言由 `["list_files", "read_file", "write_file"]`（3 个）改为 `["list_files", "read_file", "read_source", "write_file"]`（4 个）。

### 4.3 被否决的方案

- 方案 1（v1 处理）：v1 orchestrator 自己读文件 inline 到 prompt → 否决，违反 ReAct 精神。
- 方案 1（CLI 处理）：CLI 适配层把文件内容读进 prompt，WikiAgent 完全不碰外部文件 → 否决，破坏 agentic。
- v1 OrchestratorAgent 的同步改造（把 intent.files 列进 prompt 让 LLM 用 read_source 读）→ 否决，用户选「先不动 v1，只保 v2 走通」。

## 5. 第二轮反思：read_source 破坏沙箱

### 5.1 用户原话方向

> 「WikiAgent 里增加 read_source 工具读取沙箱外数据，破坏了沙箱存在的意义」
> 「可以在 wiki 沙箱目录下新建 staging 目录，只允许 read_source 读 wiki_sandbox/staging/ 下的文件」
> 「由主 agent 负责调用工具，把要入库的文件复制到 wiki/staging 目录下」
> 「research agent 里也要调用工具 save_report 把报告写入 reports/下」

### 5.2 确定的数据流

```
用户外部文件 ──import_files──► uploads/  ─┐
                                          ├──stage_files──► wiki/staging/ ──read_source──► WikiAgent
ResearchAgent.save_report ──► reports/ ─┘
```

- `import_files` 是**唯一**允许接外部 path 的工具——所有外部入口收敛到这一个口子。
- `read_source` **只**读 `staging/` —— 沙箱原则保持完整。
- `stage_files` 只接受 `reports/` 或 `uploads/` 前缀的工作区路径。

### 5.3 关键设计决策（来自 AskUserQuestion）

- **save_report 机制**：LLM 主动调 + 代码兜底（被否决：仅 LLM 主动调 / 代码自动落盘不加工具）。
- **reports/ 路径**：平铺 `reports/<filename>`（被否决：`reports/<session_id>/<filename>` 因为 v2 dispatch 不走 SessionManager；`reports/research/<filename>` 因为多余）。
- **stage_files 接受**：`reports/` + `uploads/` 双工作区（用户加入 uploads/ 概念；被否决：接任意 path / `reports/` + 用户 home）。
- **read_source 后缀范围**：保持 `.md/.markdown/.txt/.rst`（被否决：扩到 `.json/.code` 头重脚轻）。
- **推进节奏**：分 3 步，每步独立闭环（被否决：一气呵成、分 2 步），合 `feedback_impl_style`。

## 6. Step 1 实施：read_source 沙箱化

### 6.1 改动

- `core/tools.py` 的 `ReadSourceTool` 改造：
  - 从继承 `Tool` 改为继承 `FileTool`，构造接受 `root`（= `wiki_root`）。
  - 新增 `STAGING_PREFIX = "staging/"`，`execute(path)` 第一道校验拒绝非 `staging/` 前缀。
  - 复用父类 `_resolve(path)` 做沙箱兜底（防 `staging/../../outside.md` 这种穿越）。
  - 保留 `ALLOWED_SUFFIXES = {".md", ".markdown", ".txt", ".rst"}`（防御性兜底，stage 时应已校验）。
  - 保留 `MAX_BYTES = 1024 * 1024`。
- `core/tools.py` 的 `build_wiki_registry`：从 `ReadSourceTool()` 改为 `ReadSourceTool(root)`。
- `agents/wiki_schema.py`：
  - read_source 工具描述改为「读取 wiki/staging/ 下的待归档文本文件……path 必须以 staging/ 开头……配合 list_files('staging') 使用」。
  - 工作流程 step 0「原料来源判断」三态改为：内联内容 / staging 里有文件 / staging 空。

### 6.2 测试

- `tests/check_wiki_agent.py`：
  - `scenario_read_source`：在 `wiki/staging/rag_note.md` 预置原料，trajectory 是 `list_files("staging") → read_source("staging/rag_note.md") → write_file → text`。
  - `scenario_read_source_rejects`：覆盖三种拒绝路径
    - 非 `staging/` 前缀：`AI/transformer.md` → `Error: read_source 只允许读 staging/ 下的文件`
    - `staging/` 内不存在：`staging/nope.md` → `Error: 文件不存在`
    - 路径穿越：`staging/../../outside.md` → `Error: 路径 ... 在 wiki 沙箱外，已拒绝访问`（沙箱兜底）
- 测试设计踩坑：最初用 `../outside.md` 测穿越——错的，它不以 `staging/` 开头，会被**第一道前缀检查**直接拒，触不到沙箱。必须用形式合规但 resolve 后越界的 `staging/../../outside.md`。

## 7. Step 2 实施：ResearchAgent save_report + reports/ 落盘

### 7.1 新增 SaveReportTool（`agents/research_tools.py`）

- `DEFAULT_ROOT = "reports"`
- `OBS_PATTERN = re.compile(r"^已保存 (\S+)（\d+ 字符）$")`
- `parse_obs_path(obs)` 类方法：从 obs 解出路径供 ResearchAgent 读取。
- `execute(filename, content)` 校验：
  - 非空
  - 不含 `/` 或 `\\` 或 `..`
  - 必须以 `.md` 结尾
- 同名冲突：`target = self.root / f"{target.stem}-{ts}.md"`（`ts = time.strftime("%Y%m%d-%H%M%S")`）。
- `_display_path` 优先用 `relative_to(Path.cwd())`，失败时退回绝对路径。
- 父目录 `mkdir(parents=True, exist_ok=True)` 自动创建。

### 7.2 `build_research_registry` 增加 `reports_root` 参数

- 默认 `None` → SaveReportTool 用 `DEFAULT_ROOT = "reports"`。

### 7.3 ResearchAgent 循环改造

- 新状态变量：`artifact_path: Optional[str] = None`。
- `reports_root = Path(kwargs.get("reports_root") or SaveReportTool.DEFAULT_ROOT)`。
- 工具调用处理分两支：
  - `call.name == "save_report"`：用 `SaveReportTool.parse_obs_path(obs)` 解析路径，覆盖 `artifact_path`；若 `content_arg` 非空则 `final_content = content_arg`（**save_report 的 content 是权威**）。
  - 其它工具：算入 `retrieval_calls` / `retrieval_hits`。
- 末轮处理：`if not resp.tool_calls: if not final_content: final_content = resp.content`（**末轮 text 优先级低于 save_report 提供的 content**）。
- **代码兜底落盘**（循环结束后）：`if artifact_path is None and final_content.strip():` → 调 `_fallback_save(reports_root, task, final_content)`。
  - 文件名 `auto-{slug}-{ts}.md`，slug 从 `task` 前 30 字符 `re.sub(r"[^\w\-]+", "-", ...)`。
  - log 输出「LLM 未调 save_report，代码兜底落盘 → ...」。
- `result_meta` 仅当 `artifact_path` 非 None 时加入 `artifact_path` 字段。
- `_describe_action` / `_summarize_obs` 加 `save_report` 分支。

### 7.4 系统 prompt 强约束

- `SYSTEM_PROMPT_TEMPLATE` 加 save_report 工具描述。
- 新工作流程：
  1. 检索
  2. 写最终 markdown 报告
  3. **必须调 save_report(filename, content=完整报告)** —— 「契约要求，Coordinator 要拿落盘路径转交给下游 agent；不落盘等于工作未完成」
  4. save_report 后下一轮直接结束（text content 简短确认即可）

### 7.5 测试改动（`tests/check_research_spoke.py`）

- 引入 `import tempfile, contextlib`。
- `main()` 包一层 `with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):` —— 所有 case 共用 tmpdir，避免兜底落盘污染项目 `reports/`。
- 多 case 共用 tmpdir 不冲突，因为兜底文件名含 task slug + ts，LLM 调用的 filename 在不同 case trajectory 里互不重名。
- 新增 `case_save_report`：smart trajectory 包含 `save_report(filename="rl-survey.md", content=SAMPLE_FINAL)` + 末轮短确认；断言 observation 含 `artifact: reports/rl-survey.md`，且 `report:` 段是 `SAMPLE_FINAL`（**末轮短文本不覆盖**）。
- 新增 `case_fallback_save`：trajectory 不调 save_report，末轮直接 text → 兜底落盘；断言 artifact 行存在，路径以 `reports/auto-` 开头，文件真实存在。
- 现有 4 个 case 也间接触发兜底（trajectory 都不调 save_report），但断言基于 obs substr 匹配，artifact 行多了不破坏。

### 7.6 已知语义瑕疵（未决）

- `case_degenerate` 检索全空时，final_content 是「未找到相关资料，无法生成完整报告」，仍触发兜底落盘——语义上不对（degenerate 不该落盘），但 step 2 不深究。step 3 之后可让 degenerate 不 save。

## 8. Step 3 实施：Coordinator import_files / stage_files

### 8.1 新建 `core/staging.py`

- 常量：`MAX_BYTES = 1024 * 1024`，`ALLOWED_SUFFIXES = {".md", ".markdown", ".txt", ".rst"}`（与 ReadSourceTool 对齐）。
- 辅助函数：
  - `_ts_suffix()` → `time.strftime("%Y%m%d-%H%M%S")`
  - `_display(p)` → 优先 `relative_to(Path.cwd())`
  - `_resolve_with_conflict(target)` → 同名冲突时 `target.with_name(f"{stem}-{_ts_suffix()}{suffix}")`

#### ImportFilesTool

- `DEFAULT_UPLOADS = "uploads"`
- `execute(paths: List[str])`：
  - 空列表 → `Error: paths 不能为空`
  - 每个路径：`Path(raw).expanduser().resolve()`，校验：存在 / 后缀 / 大小
  - `shutil.copy2(src, target)`
  - 返回多行字符串，每行 `✓ <raw> → <display>` 或 `✗ <raw>: <reason>`

#### StageFilesTool

- `DEFAULT_WIKI = "wiki"`，`DEFAULT_REPORTS = "reports"`，`DEFAULT_UPLOADS = "uploads"`
- `ALLOWED_PREFIXES = ("reports/", "uploads/")`
- `self.staging = self.wiki_root / "staging"`
- `execute(paths)`：
  - 前缀检查（rejects: `只允许 reports/ 或 uploads/ 路径`）
  - `Path(raw).resolve()` → 用 `_under(src, root)` 判断在 reports_root 或 uploads_root 之下（rejects: `解析后越界`）
  - 文件存在 / 大小校验
  - 返回路径形式 `staging/<basename>` —— **正好匹配 WikiAgent.read_source 期望的 `staging/` 前缀**

### 8.2 `agents/coordinator_agent.py` 改造

- 工具表：从 `ToolRegistry([dispatch])` 改为 `ToolRegistry([dispatch, import_tool, stage_tool])`。
- `SCHEMA_TEMPLATE` 重写：
  - 三个工具的描述（dispatch_agent / import_files / stage_files）
  - 「跨 agent 数据流的标准模式」三条：
    - 调研 → dispatch_agent(researcher, ...) → 拿 artifact: reports/xxx.md
    - 归档 researcher 产出 → stage_files → dispatch_agent(wiki_curator, "归档 staging/ 下的 xxx.md")
    - 归档用户外部文件 → import_files → stage_files → dispatch_agent(wiki_curator, ...)
  - 派 wiki_curator 前**必须**先 stage_files
- 循环改造：tool_call 分两类
  - `kind = "dispatch"` if `call.name == "dispatch_agent"` else `kind = "local"`
  - `label = call.arguments.get("agent", "?")` if dispatch else `call.name`
  - `run_one(call, kind, label, spoke_id)`：dispatch 走 `dispatch.dispatch(...)`，local 走 `tools.execute(call)`
  - 本地工具的 obs 同样回填 messages
  - `spokes_used` 只在 `kind == "dispatch" and not obs.startswith("Error:")` 时 append
- **bug fix**：原 L203 用 `agent_name`（while 循环残留变量，多 dispatch 或 local-only 时会取错值或 NameError），改用 `label`。

### 8.3 测试

- `tests/check_staging.py`（新建）6 场景：
  - `case_import_basic`：外部 `.md` → `uploads/`，obs 含 `✓ ... → uploads/note.md`
  - `case_import_rejects`：不存在 / 后缀拒绝（`secret.bin`）/ 超大（`MAX_BYTES + 1`）/ 空列表
  - `case_import_conflict`：同名冲突 → `note.md` + `note-<ts>.md`，第二份内容是修改后的
  - `case_stage_basic`：`reports/` 与 `uploads/` 文件 → `wiki/staging/`
  - `case_stage_rejects`：非前缀（`evil.md`）/ 不存在（`reports/nope.md`）/ 越界（`reports/../evil.md`）
  - `case_end_to_end`：import → stage 双步联动，路径互相承接
- `tests/check_coordinator.py` 加 case 8：`import_files` → `stage_files` → `dispatch_agent(echo)` → text 混排。
  - 断言 `spokes_used == ["echo"]`（本地工具不计入）
  - 真实文件验证：`uploads/ext.md` + `wiki/staging/ext.md` 都存在
  - action 日志 `spoke` 标签：`"import_files"` / `"stage_files"` / `"echo"` 都在。
- 用 `contextlib.chdir(tmp)` 隔离 cwd。

## 9. 测试现状

- **12 个 `tests/check_*.py` 全部通过**：
  - check_agents / check_coordinator / check_dispatch / check_intent / check_orchestrator / check_research_spoke / check_session / check_staging / check_tool_calling / check_tools / check_wiki_agent / check_wiki_index
- `check_research_spoke.py` 现含 6 个 case（原 4 + 2 新）。
- `check_wiki_agent.py` 现含 5 个场景（旧 4 含改写 + 1 删除替换）。
- `check_staging.py` 6 个新场景。
- `check_coordinator.py` 新增 case 8。

## 10. 最终 commit

- Hash：`18941b7`
- 标题：`feat(v2): artifact 工作流（reports/uploads/staging）+ WikiAgent 入口对齐`
- 13 个文件 / +911 / −110
- 新增：`core/staging.py`、`tests/check_staging.py`
- 已 push 到 `origin/main`（`git@github.com:QingQingS/agentry-shell.git`），范围 `8c3b5ae..18941b7`，含本次和之前 3 个未推的 v2 commit。

## 11. 显式未提交、未做的事

- `CLAUDE.md`（untracked，与本次重构无关，按预期没进 commit）
- `trace.log`（untracked，运行时日志，未提交）

## 12. 未决问题 / 后续要做

- `core/registry.py` 的 `wiki_curator.input_contract` 仍写「prompt = 归档指令，含要处理的 .md 文件路径……」—— 已过时，待「重新设计每个 agent 的结构化 output」时一起改（用户明确说 step 内**先不动 registry.py**）。
- `core/intent.py` 的 `IntentResult.files` 字段已沦为废纸（WikiAgent 不再读它），等 v1 cutover 一并清。
- v1 `OrchestratorAgent` 暂不动，`orchestrator_agent.py:85` 仍写 `worker.run(task, files=intent.files)`；`files=` kwarg 被 WikiAgent 默默吞掉，但 prompt 本身含路径文字，LLM 用 read_source 自决读，**碰巧**仍走通。
- `core/dispatch.py` 的 observation 仍带 `---\nreport:\n<完整 markdown>` 段。step 3 之后 artifact_path 可作为下游消费的主要凭据，未来可改为按 artifact_path 优先、report 段可选。
- ResearchAgent `degenerate` 状态仍会兜底落盘（检索全空时把「未找到相关资料」也存进 `reports/auto-*.md`），语义不完美。未来可让 degenerate 跳过兜底。
- 长期改进建议（未实施）：
  - Post-hook 对账：`DispatchAgentTool._run_isolated` 拿到 spoke result 后校验「契约-事实」一致性（如 spec 声明 `produces_artifact: bool` 但实际 `artifact_path` 缺失则降级）。
  - Pre-hook 校验：dispatch 前校验 prompt 是否满足下游 spec（如 wiki_curator validator 扫 prompt 中 `*.md` 路径是否 stat 通过）。
  - Artifact 一等公民：从 path 字符串升级为 `Artifact(id, kind, content, path_if_persisted, hash)`，下游用 `@artifact:<id>` 引用，框架做 staging。
  - 契约 derive：让 `AgentSpec` 的 input/output_contract 从代码 schema 自动生成而非手写，避免 doc rot。
  - 失败短路：观察连续相同错误（如 wiki_curator 连续 2 次「文件不存在」）自动改写 prompt 或 fallback。

## 13. 协作约定（本会话沉淀）

- `feedback_impl_style`：每步必须独立可执行闭环；先设计再编码。本会话三步重构严格遵循。
- `feedback_commit_habit`：检查点主动小步提交。本次最终打总 commit 是用户明确要求。
- 关键设计分叉用 `AskUserQuestion` 收敛而非自己拍板（本会话用了 4 次：README 语言/位置、read_source 加不加、v1 改造方式、save_report 机制 / reports 路径 / stage_files 输入 / 推进节奏）。
- Memory `MEMORY.md` 是索引，不是内容容器；index 行 ≤ 200，单行 < 150 字符。
- README 风格保持英文一致，不混中文。
- 沙箱原则不可破——这是本次 staging 重构的核心动因；任何"破沙箱读外部"的方案应视为反模式。


---

# case-facts.md（续）：会话二 —— wiki_curator pre-hook 化，消除 staging 重复写入 bug（2026-05-30/31）

## 14. 触发本次会话的 bug 现场

- 用户测试时发现：派发 `wiki_curator` **失败后会重新复制一份文件到 `wiki/staging/`**（带时间戳的副本）。
- 日志原文佐证链：`[wiki_curator] status=error`（超时）→ LLM「wiki_curator 又超时了。可能是三份报告内容太长导致处理超时。让我试试逐个归档」→ `stage_files(1 paths)` → `✓ reports/rl_latest_advances.md → staging/rl_latest_advances-20260529-205635.md` → LLM「staging 又复制了一份（带时间戳）。没关系，让我直接用这个新 staging 文件来归档」→ 将错就错继续 `dispatch_agent(wiki_curator)`。

## 15. 根因

- **直接根因**：`core/staging.py` 的 `_resolve_with_conflict(target)`（staging.py:49-53）在目标已存在时追加时间戳后缀避免覆盖（`target.with_name(f"{target.stem}-{_ts_suffix()}{target.suffix}")`）。这个「防覆盖」语义对 `stage_files` 写 `staging/` 是错的：第一次 stage 出 `staging/rl_latest_advances.md`；`dispatch_agent(wiki_curator)` 超时 → `status=error`；Coordinator 重试再次 `stage_files` 同一份 report → 目标已存在 → 走冲突分支 → 复制成 `staging/rl_latest_advances-20260529-205635.md` → staging 里两份。
- **比「多一份文件」更糟的自我恶化机制**：`staging/` 是 wiki_curator 唯一能读的目录，WikiAgent ingest 时读的文件越多、上下文越大。失败 → 重试 re-stage → staging 多一份 → 下次 dispatch 读到更多文件 / 更大上下文 → 更容易超时 → 再重试 → 再多一份……把一次超时放大成雪崩。`_resolve_with_conflict` 的「防覆盖」本意在重试场景下把幂等操作变成了倍增操作。正好对上日志里「三份报告内容太长导致处理超时」。
- **设计层面的根本矛盾**：`ImportFilesTool` 和 `StageFilesTool` 共用同一个 `_resolve_with_conflict`，但两者性质相反——`reports/`、`uploads/` 是归档性存储，防覆盖（加时间戳）是对的；`wiki/staging/` 是临时交接缓冲区，本该可被幂等重写，在这里「防覆盖」恰恰制造垃圾。

## 16. 已定决策（承重）

### 16.1 方案选型

- **选定方案：把 staging 从 Coordinator 的工具降级成 wiki_curator 派发时的内部 pre-hook**。
- **被否决方案 (a)**：只改 `stage_files` 的写语义为覆盖/幂等，但仍把它保留为 Coordinator 的工具。
- **被否决方案 (b)**：在 dispatch 成功/失败后清理 staging。
- 选 pre-hook 方案的理由：它更彻底——Coordinator 再也碰不到 staging，从「能力层面」消除「换个名字重写」这一动作，同时解决「失败重试重复写入」和「prompt 诱导 LLM 乱搬」两件事，而不是靠 prompt 规训 LLM 别犯错。

### 16.2 hook 机制（pluggable，不破坏现有抽象）

- **staging 对 Coordinator 完全隐身**：它不再调 `stage_files`，SCHEMA prompt 里不再出现 `staging/` 字样；它只说「归档这几个文件」并指向 `reports/`、`uploads/` 路径。
- **hook 挂在 spoke 的 `AgentSpec` 上，对 Coordinator 不可见**——hook 是「派发某个 agent」的内部步骤，Coordinator 心智模型里只有「我调 wiki_curator」。
- **被否决**：在 `core/dispatch.py` 里写 `if agent == "wiki_curator": pre_hook(...)` 的硬编码特判——那是在编排层重新引入硬编码路由，正是本项目「无硬编码路由 / pluggable」卖点最忌讳的。
- **dispatch 通用地遍历 `spec.pre_hooks` 执行**，dispatch 永远不认识具体是哪个 agent。
- **`pre_hooks` 用 `list` 而非单个 callable**：为将来对称地加 `post_hooks` 铺路（pre 管「喂进去之前」改写入参/校验/短路，post 管「拿出来之后」改写产出/收尾）。会话一里讨论过的「成功归档后清理 staging」天然就是 wiki_curator 的一个未来 post_hook，架构已留好位置。
- **用户确认的伪代码心智模型**（与实现一致）：`dispatch_agent(agent, prompt, context=None, files=None)` 内构造 `payload = {prompt, context, files}`，`for hook in spec.pre_hooks: r = hook(payload); if r ...短路`，最后 `run_agent(agent, payload)`，agent 看到的是改写后的 payload。

### 16.3 hook 返回语义

- **收紧为一条规则**：hook 原地改写 `payload`（in-place），成功返回 `None`，要中止派发就返回错误字符串。
- **被否决写法**：`if r is not None and r.status != "ok"`（最初伪代码用 `Result(status=...)`）——「成功」会有「返回 None」和「返回 ok 的 Result」两种表达，留歧义。简化成「返回错误字符串 = 短路；返回 None = 成功」。
- **hook 短路时复用 dispatch 现有的 `_format(...status="error"...)`**，Coordinator 看到的 observation 和 spoke 真失败一模一样（`[wiki_curator] status=error`），它无需区分是 hook 拦的还是 spoke 跑挂的，照常临场决策。

### 16.4 结构化 files 参数（反转 v2 旧决策）

- **`dispatch_agent` 工具 schema 加可选 `files` 数组参数**：只有 wiki_curator 用，其它 spoke 忽略；靠 spec 的 `input_contract` 文档化何时给。
- 理由：pre-hook 必须机械拿到路径，不能靠 LLM 把路径塞进 prompt 散文里再正则抠。
- LLM 调用形如 `dispatch_agent(agent="wiki_curator", prompt="把这两篇归档进 wiki，按主题归类", files=["reports/rag.md", "reports/vecdb.md"])`——自然语言只写归档意图，路径走结构化字段。
- **这反转了 v2 早先的决策**：v2 当初特意把 spoke 入参统一成 `(prompt, context)`，并计划让 WikiAgent 用正则从 prompt 抠 `\S+\.md`（会话一记忆里的「步6」）。本次确认反转——正则抠散文正是这次要消灭的脆弱性。

## 17. 约定（实现细节）

### 17.1 `core/staging.py`

- **抽出纯函数 `stage_one(src, *, reports_root, uploads_root, staging_root) -> Path`**：把单个工作区文件幂等搬进 staging/。
  - 校验 `src` 以 `reports/` 或 `uploads/` 开头，且 `Path(src).resolve()` 后仍在对应根之下（沙箱，复用 `_under`）。
  - 文件存在 / 大小 ≤ `MAX_BYTES (1 MiB)` 校验。
  - 失败一律 `raise StageError`（**新增异常类**）。
- **拍平命名**：`dest = staging_root / src.replace("/", "__")`，如 `reports/a/b.md` → `reports__a__b.md`。源路径编码进目标名 → 同一源永远映射到同一目标名（时间戳变体不再可能）；不同源不会撞名（除非确实同一路径）。
- **幂等判定用内容比对**：`filecmp.cmp(dest, resolved, shallow=False)`。目标已存在且内容相同 → 跳过 copy 直接返回（重试安全）；同名异内容 → `raise StageError("拍平命名碰撞：... 与已暂存文件同名但内容不同")`（机械故障，报错而非覆盖/加时间戳）。
- **新增 pre-hook `stage_wiki_inputs(payload) -> Optional[str]`**：读 `payload["files"]`，逐个 `stage_one`，成功后把 `payload["files"]` 就地改写成 staging 内裸文件名（`dest.name`，因 `read_source` 以 staging 为根，传裸文件名）；`StageError` 转成 `"staging 失败：..."` 字符串短路返回；`files` 为空时返回 `None` 无操作（原文可能已在 prompt/context 里）。
- **根目录走 cwd 相对默认**：`./reports`、`./uploads`、`./wiki/staging`，与历史 `StageFilesTool` 一致。hook 签名只 `(payload)`、不带 config——YAGNI，将来真需要再扩成 `hook(payload, ctx)`。
- **删除 `StageFilesTool` 类整体**；`ImportFilesTool` 保留（外部文件入口仍需要，其同名冲突加时间戳行为不变——那是归档性存储，正确）。

### 17.2 `core/registry.py`

- `AgentSpec` 新增字段 `pre_hooks: List[PreHook] = field(default_factory=list)`；新增类型别名 `PreHook = Callable[[dict], Optional[str]]`（注释里同时为将来的 `post_hooks: Callable[[dict, str], Optional[str]]` 留对称说明）。
- `from dataclasses import dataclass, field`（补 `field`）。
- `from core.staging import stage_wiki_inputs`（新增 import）。
- wiki_curator spec 注册 `pre_hooks=[stage_wiki_inputs]`；researcher spec 不挂（`pre_hooks == []`）。
- wiki_curator 的 `input_contract` 重写：从「前置：派它之前必须先用 stage_files...」改为「prompt = 归档意图（不必写文件路径）；files = 要归档的 reports/ 或 uploads/ 文件路径列表（系统会在派发时自动转入 staging/ 并交给它读取，你不必也无法自己搬运）；context = 可选背景」。

### 17.3 `core/dispatch.py`

- `dispatch_agent` 工具 schema `properties` 加 `files`（array of string，可选），描述里说明「归档类 agent 用它指明待处理文件——只写路径，不要塞进 prompt 散文里」「该 agent 看到的实际路径可能被其 pre-hook 改写」。
- `execute(self, agent, prompt, context="", files=None)` 与 `dispatch(self, agent, prompt, context="", files=None, on_event=None)` 都加 `files` 形参。
- `dispatch` 内：构造 `payload = {"prompt": prompt, "context": context, "files": list(files or [])}`，按序 `for hook in spec.pre_hooks:`，`try: err = hook(payload) except Exception as e: err = "pre-hook 异常: ..."`（hook 自身抛异常也兜成 observation），`if err is not None: return self._format(spec.name, "error", err, None, None, report=None)`。hook 全过后才 `spec.factory(...)` 构造实例。
- `_run_isolated` 加 `files=None` 形参，`run_agent(instance, prompt, context=context, files=list(files or []))`。
- **零成本透传**：runner.py:26 本就 `async for event in agent.run(task, **kwargs)` 原样转 kwargs，不用改 `core/runner.py`。researcher 不读 `files` kwarg → 即使误传也被无害忽略。

### 17.4 `agents/coordinator_agent.py`

- `from core.staging import ImportFilesTool`（删掉 `StageFilesTool` import）。
- 删 `stage_tool = StageFilesTool()`；`ToolRegistry` 从 `[dispatch, import_tool, stage_tool, wiki_search, wiki_read]` 改为 `[dispatch, import_tool, wiki_search, wiki_read]`。
- `dispatch.dispatch(...)` 调用处加 `files=call.arguments.get("files")`。
- `SCHEMA_TEMPLATE` 改三处：① 删 `stage_files(paths)` 整个工具段；② 删「**派 wiki_curator 前必须先调本工具**」那行；③ `dispatch_agent` 工具说明加 `files` 参数（「归档文件时，把文件路径放进 files，不要塞进 prompt 散文里」）。
- 「跨 agent 数据流的标准模式」两条 recipe 重写：归档 researcher 产出 → `dispatch_agent(wiki_curator, prompt="把这篇归档进 wiki，按主题归类", files=["reports/xxx.md"])`（「文件搬运由系统在派发时完成，你不必（也无法）自己搬」）；归档外部文件 → `import_files([...])` → `dispatch_agent(wiki_curator, prompt="...", files=["uploads/xxx.md"])`，中间不再有 stage_files。
- 两处代码注释里「import_files / stage_files → 本地工具」更新为「import_files / wiki_search / read_file → 本地工具」。

### 17.5 `agents/wiki_agent.py`

- `run` 读 `files = [f for f in (kwargs.get("files") or []) if f]`（结构化拿到，不再从 prompt 散文正则抠路径）。
- `_format_input` 多收 `files: List[str]` 参数，非空时在 user prompt 里加「待归档的源文档已就位（在 staging/ 内）。逐个用 read_source(path) 读取原文后归档，path 用下面的文件名：」并逐行列文件名。

### 17.6 文档注释同步

- `core/tools.py` 的 `ReadSourceTool` docstring：「stage_files → wiki/staging/」改为「(pre-hook) → wiki/staging/，搬运由 wiki_curator 派发时的 pre-hook stage_wiki_inputs 完成，幂等，Coordinator 不经手」。
- `agents/wiki_schema.py` 工作流第 0 步：「外部文件需要先由上游 agent 经 stage_files 复制到 staging/」改为「待归档文件由系统在派发时放入 staging/」。

### 17.7 测试

- **重写 `tests/check_staging.py`**：覆盖 `ImportFilesTool`（合法入库 / 后缀白名单 / 冲突加时间戳，行为不变）；`stage_one`（拍平命名 `reports/r1.md`→`reports__r1.md`、同内容幂等只一份、同名异内容 `StageError("内容不同")`、非法前缀 / 源不存在拒绝）；`stage_wiki_inputs`（改写 `payload['files']` 成 staging 文件名、重复派发幂等 staging 文件数不增不出现时间戳变体、非法源短路返回「staging 失败」、空 files 返回 None）。
- **重写 `tests/check_dispatch.py`**：用 `FakeAgent` 注册进 registry，断言 status=ok/summary/artifact/report、错误转 observation、未知 agent → error、**files 透传给 spoke**、**pre_hook 改写 `payload['files']` 后 spoke 看到改写值**、**pre_hook 短路返回 error observation 且 spoke 工厂不被调用**（用 `factory_called` 标志验证）。
- **`tests/check_coordinator.py` 第 8 项**：把 `import_files → stage_files → dispatch` 三步混排改成 `import_files → dispatch_agent(echo, files=["uploads/ext.md"]) → text`，删掉对 `wiki/staging/ext.md` 落盘的断言（staging 行为移到 check_staging.py 覆盖），保留「本地工具不计入 spokes_used」「import_files 真实复制到 uploads/」「action 日志含 import_files / echo」断言。
- **结果**：全套 12 个 `tests/check_*.py` 离线测试绿（check_agents / check_coordinator / check_dispatch / check_intent / check_orchestrator / check_research_spoke / check_session / check_staging / check_tool_calling / check_tools / check_wiki_agent / check_wiki_index）。
- **额外 E2E 验证**：手写脚本连派两次 wiki_curator（模拟失败重试），确认 staging 只有一份 `reports__rl.md`、spoke 两次都看到 `["reports__rl.md"]`、无时间戳副本。

## 18. 踩过的坑

- **助手第一次挂 hook 的 Edit 匹配失败 → hook 实际没挂上**：第一次想把 `pre_hooks=[stage_wiki_inputs]` 加到 wiki_curator spec 时，`old_string` 猜成了 `output_contract="status/summary（touched 页）+ key_facts。"`——这文本在文件里根本不存在（是别处记忆里的串），Edit 报「String to replace not found」。当时没立即复查就继续推进，导致 hook 没真正注册。**靠改完后跑 `python -c "...print pre_hooks..."` 的 sanity check 发现 `wiki pre_hooks: []`**，回头补正确的 Edit 才修复（确认变成 `['stage_wiki_inputs']`）。
  - 教训：**测试可能照样绿**（check_dispatch 直接构造带 hook 的 spec、不走 `build_default_registry()`），但生产注册表里 hook 是空的——这正是 bug 会溜走的缝隙。改注册表后必须对 `build_default_registry()` 实例本身断言 hook 是否挂上。
- **zsh 下读取含中文的大文件（case-facts.md，约 13970 字符 / 277 行）一度受阻**：`cat -A` 在 macOS 报 illegal option；heredoc 里的 python 多次被 shell 解析吞掉输出；Read 工具读 `/tmp` 中转文件偶发返回空。最终靠「Write 临时脚本文件 → python 执行 → 重定向到文件 → Read 文件」绕过。

## 19. 残留 / 未决

- **staging/ 里 bug 期产生的时间戳垃圾需手工清一次**（本次只修机制不清历史）。已逐字节核对确认安全可删：`wiki/staging/rl_latest_advances-20260529-205635.md` 与 `rl_latest_advances.md` 字节完全相同；`wiki/staging/agent_latest_advances-20260529-205755.md` 与 `agent_latest_advances.md` 字节完全相同（`cmp -s` 验证）。其余 `.md` 是正常待归档文件，不动。残留是死垃圾、不会被重新 ingest（WikiAgent 只 `read_source` `files` 指定的文件，不自动扫整个 staging/）。
- **「成功归档后自动清理 staging」** 留作单独的小改进（未来 post_hook），不耦合进本次。
- **本次约 8 处文件改动尚未 commit**：`core/staging.py`、`core/registry.py`、`core/dispatch.py`、`agents/coordinator_agent.py`、`agents/wiki_agent.py`、`agents/wiki_schema.py`、`core/tools.py`、`tests/check_staging.py`、`tests/check_dispatch.py`、`tests/check_coordinator.py`。建议单独成一个干净的 bug-fix commit。
