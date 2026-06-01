# WikiAgent 开发讨论记录

> 创建：2026-05-22
> 状态：**设计讨论中，尚未开始编码**
> 上次停在：agentic 架构确认，具体实现方案下次讨论

---

## 一、定位与愿景

### 三层知识体系

```
用户
  ↓ 提问
ChatAgent          ← "前台"：理解问题、综合作答
  ├─ 调 ResearchAgent  ← "图书馆进新书"：检索外部、生成报告（为 ChatAgent 服务）
  └─ 查 WikiAgent       ← "翻自己笔记"：查本地已整理的知识（为 ChatAgent 服务）
        ↑
     WikiAgent（纯后台）：接收 .md 文件 → 整理归档 → 维护知识结构
```

WikiAgent 是**纯粹的知识管理员**：
- 不和用户直接对话
- 不负责回答问题（那是 ChatAgent 的事）
- 只负责：接收文档 → 理解内容 → 归档整理 → 维护知识结构

### 设计哲学

> 智能放在 SCHEMA + prompt（散文/规范）里，代码极薄——代码只给 LLM 读写文件的工具，由 LLM 自己决定建哪些页、改哪些页、修哪些链接。

---

## 二、已确定的设计决策

### 1. 触发方式
- **显式触发**，由用户发起（例："把 ./reports/transformer.md 存入 wiki"）
- 输入：一个或多个 .md 文件路径（来源不限：研究报告、用户自备笔记均可）
- 与 ResearchAgent 完全解耦——接口统一为"给我 .md 文件"

### 2. wiki 页面是「主题中心」而非「文件镜像」

**关键认知**：wiki 页面不是输入文件的 1:1 拷贝，而是以知识主题为中心的积累点。

- 多个来源文件可以汇聚到同一个 wiki 页面（同一主题的多次学习）
- 一个来源文件可能拆分到多个 wiki 页面（跨主题文档）
- LLM 自主决定归属，不做强制单一分类

**wiki 页面结构（YAML frontmatter + markdown）**：
```markdown
---
title: Transformer
category: AI
created: 2026-05-22
updated: 2026-05-22
sources: 2
entities:
  - Attention Mechanism
  - Multi-head Attention
  - BERT
---

## 摘要
[LLM 综合所有来源生成的知识摘要]

## 来源文件
- [transformer_note1.md](/path/to/file) — 2026-05-22
- [transformer_note2.md](/path/to/file) — 2026-05-22

## 知识内容
[结构化知识正文]
```

### 3. index.md 是 wiki 的脊梁

**面向内容的目录**，每个页面一条记录，按类别组织，LLM 每次 ingest 时更新：

```markdown
# Wiki Index
*最近更新：2026-05-22*

## AI
- [Transformer](AI/transformer.md) — Transformer架构及注意力机制 | 实体: Attention, BERT | 来源: 2 | 2026-05-22

## Systems
- ...
```

两个作用：
- LLM 的**环顾入口**（ingest 时先读这里，了解现有知识结构）
- 未来 ChatAgent 的**检索入口**（"wiki 里有没有 X 相关的页面"）

### 4. 一致性维护：有界策略，不全扫

只动相关页面 + 更新 index.md，不重扫全库。通过 index.md 保持全局一致性。

### 5. 相关性判断机制（讨论一结论）

**错误做法**：让 LLM "判断这份资料属于哪个主题"——强制单一归类，必然出错。

**正确思路**：分两阶段
1. **第一阶段（无 LLM）**：用实体列表做集合交集，从 index.md 筛出候选页面
2. **第二阶段（LLM）**：对候选页面批量打分（0-1 + 理由），一次 LLM 调用返回所有候选的评分

打分 prompt 核心逻辑：
> "给定这份资料的摘要 [summary]，以及这个 wiki 页面的现有内容 [page]，评分 0–1：1.0 代表资料实质性丰富了这个页面，0.7 代表有意义的重叠和新视角，0.5 代表切线相关但不会实质改变页面，0.0 代表不相关。返回分数和一句理由。"

阈值规则：
- score ≥ 0.7 → 更新该页面
- 所有候选 < 0.5 → 新建页面

**注意**：以上打分逻辑描述的是"相关性判断的智能"，不是说实现一定要用固定流水线——具体执行方式由 agentic 设计决定（见下）。

### 6. WikiAgent 必须是真正 agentic 的 agent ⚠️

**这是本次讨论最重要的结论。**

WikiAgent 是项目第一个真正 agentic 的 agent（ReAct 式工具循环），原因：

**执行序列本身依赖于运行时发现的信息**：
- 要读几个已有页面？ → 取决于 index.md 里有什么
- 要更新几个页面？ → 取决于读到的内容与新文档的关系
- 要新建几个页面？ → 取决于现有知识结构的空白在哪
- 一份文档可能涉及 3 个主题，需写 3 个页面——Python 无法预知这个 3

对比 ResearchAgent（固定流水线是合理的）：
```
ResearchAgent: 分解子问题 → 检索 → 汇总 → 报告  （序列固定，Python 可编排）
WikiAgent:     读 index → 读 N 个页面 → 写 M 个页面 → 更新 index （N、M 由 LLM 决定）
```

如果用固定流水线，控制权从 LLM 转移给了程序员（阈值、pre-filter 规则都是程序员写死的），agentic 就消失了。

**真正 agentic 的 WikiAgent**：
```
LLM 拿到：工具集 + SCHEMA.md + 输入文档
↓
自主执行：
  读 index.md → 读相关页（自己决定读哪几个）→ 决策（更新/新建哪些页）
  → 写页面（自己决定写几个）→ 更新 index.md → 停止
```

### 7. Tool 层是强前置

WikiAgent 需要的工具（全部沙箱限定在 `./wiki/`）：
- `read_file(path)` — 读 wiki 页面
- `write_file(path, content)` — 写 wiki 页面
- `list_files(dir?)` — 列出 wiki 页面
- （可选）`grep(pattern, dir?)` — 内容检索

BaseLLM 目前只有 `chat` / `chat_stream`，**没有 tool calling 支持**（Role 里预留了 `"tool"` 但未实现）。这是硬性前置工作。

---

## 三、当前已知的实现步骤方向

```
Step 1: core/tools.py         工具基类 + 4 个文件工具（沙箱 ./wiki/）
Step 2: core/llm/base.py      加 tool calling 支持（chat_with_tools / ReAct 循环）
Step 3: agents/wiki_agent.py  真正的 agentic ReAct 循环
Step 4: 集成 Orchestrator      route=wiki，IntentClassifier 加 wiki 示例
```

**尚未讨论清楚**：
- Step 2 的具体设计：BaseLLM 如何抽象 tool calling（各 provider 实现差异大）
- Step 3 的 ReAct 循环边界：何时停止、如何处理 LLM 写错文件的情况
- SCHEMA.md 的内容：给 LLM 的操作规范如何写才能让它有纪律地维护 wiki

---

## 四、读取（ChatAgent 查 wiki）

暂不设计，留待后续阶段。

## 五、知识图谱

后置功能。ingest 时做实体提取（每文档不超过 5-8 个实体，避免发散），记录在页面 frontmatter 的 `entities` 字段，为未来知识图谱留接口。

---

## 六、接入 OrchestratorAgent 的方式

IntentClassifier 加 `route=wiki` 臂（payload: `{files: [...]}`），Orchestrator 加 wiki dispatch，不动 research/chat 路径。当 route 增多且各带不同 payload 时，IntentResult 重构为 tagged union（不提前抽象）。

---

---

## 七、BaseLLM tool calling 设计（讨论三，2026-05-23 已定）

### 核心决策（3 个分叉全部拍板）

1. **ReAct 循环在 WikiAgent，不在 BaseLLM**（B 方案）。BaseLLM 只管单次 tool-enabled 调用的归一化。理由：agentic 的控制权（读几页/写几页/何时停）必须在 agent；可观测性（每步 yield log 事件）要在 agent 循环里；token 记账/工具错误/写错文件恢复都是 agent 关切。
2. **范围裁剪**：工具路径**不做流式**（后台策展不需要逐 token，流式组装 tool_calls 痛且无收益）；**先只实现 DeepSeek/OpenAI 路径**，中性类型设计成 Anthropic 可插入但本期不实现/不测（标为扩展点）。
3. **签名扩展 `chat` 而非新增 `chat_with_tools`**：`async def chat(self, messages, *, tools: Optional[List[ToolSpec]] = None, ...)`。`tools` 是具名参数（要翻译不能搭 `**kwargs` 透传）。

### 归一化数据模型（中性类型，provider 无关）

```python
@dataclass
class ToolSpec:        # 喂给 LLM 的工具定义
    name: str
    description: str
    parameters: dict   # JSON Schema

@dataclass
class ToolCall:        # LLM 要求调用
    id: str            # provider 的 call id，回填时对齐用
    name: str
    arguments: dict    # 已解析成 dict（OpenAI 回 JSON 字符串，在 provider 里解析）

# LLMResponse 增补：
    tool_calls: List[ToolCall]   # 无则空
    stop_reason: str             # 归一化 "tool_calls" | "stop"
```

Agent 只碰中性类型，永不接触 provider 原生结构。

### 最漏的缝：消息历史往返

- OpenAI/DeepSeek：assistant 消息挂 `tool_calls`；每个结果一条独立 `{role:"tool", tool_call_id, content}`。
- Anthropic：assistant `content` 是 block 数组（含 `tool_use`）；结果必须全塞进一条 user 消息的多个 `tool_result` block。

**处理**：`ChatMessage` 加两个可选字段（`tool_calls` 给 assistant 轮，`tool_call_id` 给 tool 结果轮），保持单一中性消息类型；序列化知识**下沉到各 provider**，不再假设 `to_dict()` 通用（纯文本路径 `to_dict()` 保留不动）。Anthropic 的「连续 tool 结果合并成一条 user 消息」是最易写错点，单独测。

### 实现步骤（每步独立闭环）

| 步 | 工作 | 独立验证 |
|---|---|---|
| A | 中性类型 + ChatMessage 加字段 + LLMResponse 加字段 | 类型可导入；跑现有纯文本测试确认无回归 |
| B | DeepSeek `chat(tools=...)`：请求带 schema、响应解析 tool_calls、tool 消息往返序列化 | 独立脚本：假 `add(a,b)` 工具，问「2+3」→ 确认发 tool_call → 回填 5 → 确认答 5。真实 DeepSeek API 端到端，不需 WikiAgent |

Anthropic 工具支持（若要）是独立后续步。

### 待后续细化（不在 BaseLLM 层）

- OpenAI 参数 JSON 可能畸形（LLM 偶发坏 JSON）：可恢复事件，provider 解析失败不 crash，best-effort `arguments={}`（已实现）。后续 agent 循环可决定回喂「参数无效」。

### ⚠ Step B 实现中发现的坑：思考模型的 reasoning_content（2026-05-23）

`deepseek-v4-pro` 是**思考模型**：返回 tool_call 时附带 `reasoning_content`，续接对话时**必须原样回传**该字段，否则 API 报 400（"reasoning_content in the thinking mode must be passed back"）。

**解法（已实现）**：`ChatMessage` 和 `LLMResponse` 各加一个中性可选字段 `reasoning_content`，OpenAIProvider 解析时取出、序列化 assistant 工具调用轮时回传。DeepSeek 细节锁在 provider 层，中性类型只存不解释，其他 provider 忽略。

**对未来 ReAct 循环的硬约束**：loop 往 messages 追加 assistant 工具调用轮时，**必须带上 reasoning_content**：
```python
messages.append(ChatMessage(
    role="assistant", content=resp.content,
    tool_calls=resp.tool_calls, reasoning_content=resp.reasoning_content,
))
```
忘了带 → 第二轮 400。写 WikiAgent ReAct 循环时务必记得（可考虑给 LLMResponse 加 `to_assistant_message()` 助手方法把这事封装掉，避免每处手抄）。

### Step B 落地状态（2026-05-23）

✅ Step B 已实现并验证。`core/llm/base.py` chat 加 tools 参数；`openai_provider.py` 实现工具 schema 转换 / 消息三形态序列化 / tool_call 解析 / reasoning_content 往返；`anthropic_provider.py` tools 传入时 NotImplementedError（扩展点）。`tests/check_tool_calling.py` 真实 DeepSeek 端到端通过（假 add 工具：发起调用→回填→答出 5），离线 4 套回归全绿。

### Step A 落地状态（2026-05-23）

✅ Step A 已实现并合入 master（git 仓库于本日初始化，`.env` 已 gitignore）。中性类型 ToolSpec/ToolCall + ChatMessage/LLMResponse 字段扩展全部就位，离线测试全绿，纯文本路径无回归。下一步 Step B（DeepSeek 工具路径）。

---

## 八、core/tools.py 工具层 + ./wiki/ 沙箱（讨论四，2026-05-23 已定）

4 个分叉全部按推荐拍板。

### 1. 工具的最小抽象：显式 class-based + 手写 schema

一个工具 = ToolSpec（向 LLM 广告，Step A 中性类型）+ 执行函数（吃 `arguments: dict`，吐字符串 observation）。

选**显式 class-based**（`class Tool(ABC)`，每工具一子类，JSON schema 手写在代码里），不用装饰器签名推断。理由：只有 3-4 个工具、schema 极小，手写让「喂给 LLM 的契约」一眼可见；装饰器推断是藏行为的 magic，违背裸 SDK/代码薄/重可观测的项目气质。

注册表对 ReAct 循环暴露两个口（接上 Step A 类型）：
```python
registry.specs() -> List[ToolSpec]              # 传给 llm.chat(tools=...)
await registry.execute(call: ToolCall) -> str   # 按 name 分发，返回 observation
```
这是工具层↔循环的唯一缝：agent 拿 specs 喂 LLM → 收 ToolCall → registry.execute → 拿字符串 → 包成 `ChatMessage(role="tool", tool_call_id=..., content=...)`。

### 2. 沙箱是核心安全边界（逻辑集中一处，不散落）

所有路径参数解析后必须落在 `./wiki/` 内。`WikiSandbox`（或基类 `FileTool`）持有 `root`：
```python
def _resolve(self, path: str) -> Path:
    p = (self.root / path).resolve()          # 摊平符号链接 + ..
    if not p.is_relative_to(self.root.resolve()):
        raise SandboxViolation(path)
    return p
```
要挡三类攻击：路径穿越（`../../etc/passwd`）、绝对路径（`/etc/passwd`）、符号链接逃逸。`resolve()` + `is_relative_to`（Py3.9+）是干净写法。

### 3. 错误处理（最关键决策）：工具永不向循环 raise，所有失败→observation 字符串

文件不存在/越界/坏参数都不是 bug，是**信息**——回喂给 LLM 让它自己改（去 list_files、新建、纠正路径）。这是 agentic 的核心。

分层：
- 越界在 `_resolve` 内部 raise `SandboxViolation`——**真正越界操作绝不执行**；
- `execute()` 在边界 catch 所有异常 → **yield log 事件**（可观测："拒绝越界访问 X"）→ 把错误串当 observation 返回；
- `execute()` 永远返回字符串（成功结果或 `"Error: ..."`），循环极简、永不因工具崩。

**推论**：唯一能停 ReAct 循环的是 MAX_STEPS 或 LLM 不再发 tool_call。这把「循环边界」未决点收窄了。

### 4. 工具集：先做 3 个，grep 推迟

- `read_file(path)` → 页面内容或 `"Error: 不存在"`
- `write_file(path, content)` → **整篇覆盖**（页面是 读旧→产出新全文→写回，不 append），沙箱内**自动建父目录**（LLM 按 category 建 `AI/` 等子目录）；**写入限定 `.md`**（轻量护栏，index.md 也是 md）
- `list_files(dir?)` → **递归**列出 wiki 内所有 `.md`，返回**相对 wiki 根**路径（如 `AI/transformer.md`），可直接喂回 read_file；不返回绝对路径（泄露沙箱根/混淆 LLM）
- ~~`grep`~~ → **推迟**。index.md 本就是检索入口（读 index→选候选→读页），不需全文检索；证明不够用再加（不提前抽象）

### 5. 冷启动

首次 ingest 时 `./wiki/` 或 `index.md` 不存在 → **沙箱 init 时确保 `./wiki/` 目录存在，并种一个 skeleton `index.md`**，免得 list/read 返回令人困惑的空。骨架由沙箱层种，比让 LLM 凭空建更稳。

---

### 仍未讨论清楚（下次接续）

- WikiAgent ReAct 循环边界：MAX_STEPS 取值、停止条件已基本明确（无 tool_call 或触顶）；仍需定 LLM 反复写错/兜圈子时的兜底
- SCHEMA.md 内容：给 LLM 的 wiki 维护操作规范怎么写（相关性打分逻辑、实体提取上限、何时新建 vs 更新页面）

---

### Step C 落地状态（2026-05-23）

✅ Step C 已实现并验证。`core/tools.py`：Tool/FileTool 基类 + ReadFileTool/WriteFileTool/ListFilesTool + ToolRegistry（specs() / execute(call)→str，所有失败收敛为 observation 字符串）+ build_wiki_registry（冷启动种 index.md 骨架）。`SandboxViolation` 在 `_resolve` 抛、registry 边界转字符串。`tests/check_tools.py` 离线全绿，含三类沙箱攻击（路径穿越 `../`、绝对路径、符号链接逃逸）均挡住且越界操作未执行。grep 仍按计划推迟。

注：`./wiki/` 目录在首次 ingest（或测试用临时目录）时才创建；是否纳入 git 由用户在 WikiAgent 跑起来后决定（是策展知识内容，未必算运行时产物）。

---

---

## 九、SCHEMA.md 设计（讨论五，2026-05-23 已定）

WikiAgent 的「操作手册」，按设计哲学智能在此、代码极薄。

### 放置方式
SCHEMA 内容进 **system prompt**（循环启动时 agent 注入），不放 ./wiki/ 让 LLM 用 read_file 读——规范要始终在场、不浪费循环步数。可作为 repo 文件（`agents/wiki_schema.py` 常量或 `wiki_schema.md`）加载。

### 4 个分叉决策（全按推荐拍板）
1. **相关性判断**：不强制 LLM 输出数字分数；改为**定性纪律 + 写每页前用一句话说明「为何更新/新建」**（保 agentic、省 token、给可观测性）。0.7/0.5 阈值作为引导写进 SCHEMA，不硬算。
2. **index.md 更新时机**：所有页面写完后**最后统一更新一次**（单次 ingest 是事务单元，省步数）；「index 更新完 = 结束」作为循环 **STOP 信号**。代价：中途崩溃留下不一致，本期可接受。
3. **category 体系**：LLM **自创但强制先读 index、优先复用已有 category**，index.md 当 category 权威清单压制同义发散。
4. **合并策略**：更新页面时新知识**整合进知识内容对应小节（非末尾粗暴 append）**，摘要重写以覆盖全部来源，来源文件追加条目，entities 合并去重。

### SCHEMA 要覆盖的 6 块

**A. 角色与目标**：wiki 策展员，接收 .md 文档整合进以主题为中心的知识库；不回答用户问题。

**B. 结构契约（字面精确）**——页面模板：
```markdown
---
title: <主题名>
category: <类别>
created: <YYYY-MM-DD>
updated: <YYYY-MM-DD>
sources: <已汇入来源文件数>
entities: [<实体1>, <实体2>, ...]
---

## 摘要
<综合所有来源的 2-4 句知识摘要>

## 来源文件
- <来源文件名> — <YYYY-MM-DD>

## 知识内容
<结构化正文，按子主题分节>
```
index.md 模板：
```markdown
# Wiki Index
*最近更新：<YYYY-MM-DD>*

## <类别>
- [<标题>](<相对路径>) — <一句描述> | 实体: <e1,e2> | 来源: <n> | <updated>
```
路径约定：页面在 `<category>/<slug>.md`，index.md 在根；slug 小写连字符。

**C. 工作流程（引导非死步骤，保 agentic）**：
1. 读 index.md 了解现有 category 与各页（描述/实体）
2. 每份输入文档：提炼主题 + 5-8 个关键实体
3. 凭 index 实体/描述圈候选页 → read_file 读全文
4. 判断：实质丰富→更新；切线→不动；无匹配→新建；一份文档可命中多页（跨主题拆）
5. 更新：**先读→合并（不丢旧知识）→整篇 write**，维护 frontmatter；新建：按模板 write，优先复用 category
6. 全部页面处理完，最后读 index.md 更新条目并 write 回
7. 完成，停止（不再发 tool_call）

**D. 硬纪律**：
- **更新页面前必须先 read**（write_file 整篇覆盖，不先读=毁掉整页）——头号纪律
- 主题中心非文件镜像；多源汇聚同页，跨主题拆多页
- category 优先复用 index 已有的，抑制同义发散
- 实体每文档 5-8 上限
- 有界一致性：只动相关页 + index，不重扫全库
- 只在 ./wiki/ 内、只写 .md；不删页（本期）；不回答用户问题

**E. 输入约定**：首条消息收到待归档文档的文件名 + 全文。

**F.（写进 D 即可）** 写每页前一句话理由（分叉 1）。

---

---

## 十、ReAct 循环兜底设计（讨论六，2026-05-23 已定）

### 循环骨架
```
messages = [system(SCHEMA), user(输入文档全文)]
for step in range(MAX_STEPS):
    resp = await llm.chat(messages, tools=registry.specs())
    messages.append(assistant 轮)   # 含 tool_calls + reasoning_content（回传约束！）
    if not resp.tool_calls:
        break                        # LLM 自然收尾，resp.content 即总结
    for call in resp.tool_calls:
        obs = await registry.execute(call)   # 永不抛，返回字符串
        messages.append(tool 结果轮)
        yield log 事件
```
一个 step = 一次 LLM 调用（可能含多个 tool_call，全执行后回填）。

### 4 个分叉决策
1. **MAX_STEPS = 20**（每次 ingest 的 LLM 调用上限，可配常量）。命中后：停止，emit warning，列出本次已写/改的文件（agent 跟踪 `touched_files`），**不自动修 index**（无事务）。不做强制总结调用。
2. **兜圈子检测（采纳 B）**：检测同一 `(name, args)` 重复 ≥3 次 → **注入一次 nudge**（"你已多次执行 X 得到相同结果，请改变策略或结束"）；再不停交给 MAX_STEPS 硬截。nudge 只注入一次。
3. **result 产出**：自然停止 → LLM 最后一轮 text content 即为 result；强制停止 → agent 用 `touched_files` 合成（"已写入/更新 N 页，因达步数上限提前结束"）。不额外发"请总结"调用。
4. **"写错"**：路径越界/非 .md 由工具层返 error observation、LLM 自纠（已覆盖）；**语义写错不做程序兜底**（工具层无法判断对错），靠 SCHEMA 纪律 + LLM 自律。

### 非分叉约定
- **可观测性**：每个 tool_call 按 name+结果 yield 一条 log（"读取 index.md"/"写入页面 X"/"拒绝越界 Y"）；LLM 工具轮的 text content（分叉 1 那句理由）也 yield 成 log。
- **已知边界（本期不解决）**：多文档 + 读大页面致 context 增长，先观察、触顶再说。

### Step D 设计输入已齐
工具层（第八节）+ SCHEMA（第九节）+ 兜底（第十节）全部就位，可直接实现 agents/wiki_agent.py。

---

### Step D 落地状态（2026-05-23）

✅ Step D 已实现并验证。`agents/wiki_schema.py`（WIKI_SCHEMA 常量，落地九节）+ `agents/wiki_agent.py`（WikiAgent ReAct 循环：读输入文档→SCHEMA 进 system→工具循环，reasoning_content 回传、MAX_STEPS=20、兜圈子 nudge≥3、touched_files 跟踪、每步 yield log、自然停取末轮 content 作 result）。`core/llm/__init__.py` 补导出 ToolSpec/ToolCall。`tests/check_wiki_agent.py`：离线 FakeLLM 确定性测全绿（工具分发/消息往返/touched/自然停/nudge 一次/MAX_STEPS 触顶/空输入抛错）+ `--live` 真实 DeepSeek 端到端跑通（空库→读 index→新建 AI/transformer.md→更新 index→总结）。

观察：--live 中 LLM 提取了 10 个实体，超 SCHEMA 的 5-8 上限——prompt 遵守度的小瑕疵，非代码 bug；后续可微调 SCHEMA 措辞（标为待观察，不阻塞）。

---

### Step E 落地状态（2026-05-23）—— 阶段四完成

✅ Step E 已实现并验证。`core/intent.py`：`_ROUTES` 加 "wiki"；IntentResult 加 `files: List[str]`（**保持扁平、不提前抽象 tagged union**，仅 wiki 用 files）；分类 prompt 加 wiki 路由说明 + files 字段 + 示例；`_parse` 处理 wiki（提取 files，wiki 但无 files → 安全降级 research）。`agents/orchestrator_agent.py`：import WikiAgent + `elif route=="wiki"` dispatch（透传 files，不传 session 级 wiki_root → 用 WikiAgent 默认，wiki 跨 session 共享）；write_back 走既有 else 分支（add_turn，不落报告）。

验证：`tests/check_intent.py` 加 wiki 解析用例（提取/无文件降级/非 wiki files 空）；`tests/check_orchestrator.py` 加 FakeWikiAgent + wiki dispatch 轮（透传 files、不注入背景、不新增报告、落 turn）。离线全套全绿。`--live`：真实分类器正确把「把 X.md 存进 wiki」判成 route=wiki 并抽出路径，其余 4 用例无回归。

⚠ **已知边界**：冷启动首轮无 session 上下文时 classify_intent 跳过 LLM、降级 research（与 B9 同源），故「首条消息就是 wiki 归档」会落到 research；自然流程「先研究产出报告 → 再归档」有上下文则正常命中 wiki。如需首轮即支持 wiki，需让 classify 在无上下文时仍调一次 LLM（route/files 本可只凭输入判定）——留作可选改进。

---

*WikiAgent（阶段四）全链路完成：Step A/B/C/D/E ✅。可独立运行，也已接入连续对话编排（route=wiki）。剩余可选收尾：①首轮 wiki 检测（B9 同源）②SCHEMA 实体上限措辞微调（--live 见 LLM 提了 10 个超 5-8 上限）③CLI --interactive 真实多轮 E2E 冒烟（研究→归档）。下一步可转向阶段五或先做这些收尾*
