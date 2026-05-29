# prompt

把我们这次讨论中所有已经确定的决定、约定、踩过的坑、未决问题，以条目形式保存到 case-facts.md。逐条列出，不要概括，不要省略具体的文件名、函数名、数值和被否决的方案

# case-facts.md

本次会话的逐条事实记录。覆盖：根因诊断、两轮设计调整、3 步实施细节、被否决方案、测试现状、未决问题。

---

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
