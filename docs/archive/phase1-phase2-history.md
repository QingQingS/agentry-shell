# 阶段一 & 阶段二 开发历史归档

> 此文件为归档内容，不再主动维护。
> 需要回溯某个设计决策时可在此查阅。
> 主文件：CONTEXT.md / NOTES.md

---

## 一、阶段一 & 阶段二 完整开发进展记录

1. **阶段一健康度审查**：通读所有文件并实测三入口，结论"架子健康，可信任"。识别了一份按优先级排序的"埋雷清单"，用户选择**暂不修**，直接进入阶段二
2. **LLM 抽象层**：`core/llm/` 子包，`BaseLLM` 接口、`TokenUsage`、`get_llm()` 工厂；支持 OpenAI / DeepSeek / Anthropic
3. **协议改动**：`AgentEvent.type` 中的 `cost` 替换为 `tokens`，metadata 携带 `{input/output/total_tokens, provider, model}`
4. **ChatAgent**：最小可用单轮聊天 Agent，验证 LLM 层端到端
5. **实测**：DeepSeek（`deepseek-v4-pro`）调通三入口，事件序列 `status(running) → log → tokens → result → status(done)`
6. **Retriever 抽象层**：`core/retrievers/`，`BaseRetriever` + `SearchResult`；`ArxivRetriever`（arxiv SDK + asyncio.to_thread）；`LocalFileRetriever`（txt/md/pdf，段落分块，关键词重叠评分）
7. **ResearchAgent**：完整版编排（拆子问题→ArXiv检索→逐项总结→汇总报告），DeepSeek + ArXiv 闭环通过（5 次调用约 7k tokens）
8. **流式 LLM 输出**：`BaseLLM.chat_stream()`；OpenAI/DeepSeek 用 `stream=True + stream_options include_usage`；Anthropic 用 `messages.stream()`；ResearchAgent 最终报告流式推送
9. **Tavily 接入**：`core/retrievers/tavily.py`（httpx 异步），Config 新增 `retriever` + `tavily_api_key`
10. **多源并发检索**：`asyncio.gather(return_exceptions=True)` 并发；`_merge_results()` 交叉排列+URL去重；ArXiv 429 退避重试（10s/20s/40s）

---

## 二、已关闭的 Bug

| # | 位置 | 问题 | 修复 |
|---|------|------|------|
| B1 | `websocket_manager.py` | Token 统计无会话级累计 | 增加 `_session_usage` 字典，任务完成后累加并推送 `scope=session` tokens 事件 |
| B2 | `core/retrievers/arxiv.py` + `base.py` | ArXiv 检索结果缺少发表时间和作者 | `SearchResult` 新增 `published`/`authors` 字段；ArxivRetriever 填充 |
| B3 | `agents/research_agent.py` | 报告中论文标题被翻译为中文 | system prompt 明确要求保留英文原标题 |
| B5 | `frontend/scripts.js` | 发送后输入框不清空 | `runTask()` 发送后立即 `taskInput.value = ""` |

---

## 三、参照项目路径（已用完）

- gpt-researcher 源码：`/Users/sunqingqing/projects/gpt-researcher-main/`
- `gpt_researcher/utils/llm.py` — ✅ 已参照（裸 SDK，未用 langchain）
- `gpt_researcher/retrievers/arxiv/arxiv.py` — ✅ 已参照（ArxivRetriever）
- `gpt_researcher/retrievers/tavily/tavily_search.py` — ✅ 已参照（TavilyRetriever）
- `gpt_researcher/skills/researcher.py` — ✅ 已参照（ResearchAgent 编排）
- `gpt_researcher/agent.py` — ✅ 已参照
- `backend/chat/chat.py` — 阶段三记忆系统参照（按需查阅）

---

## 四、NOTES 历史设计讨论（已完成阶段）

### 4.1 Retriever 抽象层设计决策

**核心决策**：本地文件做成真正的 Retriever，关键词检索替代向量检索（后期升级 embedding 只换后端，不改接口）。

**LocalFileRetriever 设计**：
- 索引懒加载（第一次 search() 触发）
- 段落分块（`\n\n`）：< 100 字符合并，> 500 字符再切
- 评分：词重叠 `overlap / len(query_tokens)`，纯 Python
- PDF：`fitz.open()`（pymupdf）

**本地文件路径：方案 B** — 路径在 Agent 初始化时传入，不写 `.env`。Retriever 更纯粹。

**参考代码取舍（用户提供的 RAG 代码）**：
- `RecursiveCharacterTextSplitter`：需要 langchain + 重叠冗余，不引入
- `Chroma + MMR`：需要 embedding + 向量库，阶段二不引入
- 错误处理：单个文件失败静默跳过，不全量崩溃

```python
# 用户提供的参考代码（Chroma + HuggingFace embedding 方案，留存备查）
# 见原始对话记录
```

### 4.2 ResearchAgent 设计决策

**编排深度：完整版** — 保留逐子问题总结环节。理由：教学价值，每步显式拆开且 yield 事件让前端可见进度。代价是 N=3 时约 5 次 LLM 调用。

**子问题结构化输出**：prompt 要求 JSON 数组 + json.loads 解析；失败回退行切分；再失败用原始主问题。

**英文子问题**：ArXiv 英文 query 召回更好；最终报告输出中文。

**Token 事件**：异步生成器中无法在回调里 yield，改为每次 chat() 后手动 yield tokens 事件 + 结束 yield 累计事件。

### 4.3 统一生命周期钩子（core/runner.py）

**问题**：on_start/on_finish/on_error 靠每个 Agent 自觉调，忘了就 status 永远 IDLE。

**方案**：`core/runner.py` 的 `run_agent()` 统一包装。契约反转：Agent 只 yield 领域事件 + 失败抛异常，钩子和 status 全由 runner 保证。

实测通过：EchoAgent happy path + 异常路径 + ChatAgent 真实 DeepSeek。
