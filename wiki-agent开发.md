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

- OpenAI 参数 JSON 可能畸形（LLM 偶发坏 JSON）：可恢复事件，provider 解析失败不 crash，best-effort `arguments={}` 并保留原始串，由 agent 循环决定回喂「参数无效」。放到 WikiAgent ReAct 那轮细化。

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

*下次会话继续：tool 层设计已定（第八节）。可进 Step B（DeepSeek 工具路径）实现；或先讨论 SCHEMA.md 内容 + ReAct 循环兜底*
