# WikiAgent 优化记录

> 承接阶段四（WikiAgent 已完成、已接入编排）。本文件记录 WikiAgent 投入使用后
> 发现的可观测性 / 性能问题，以及调优的设计讨论与前后效果对比。
> 开发期冻结讨论见 `wiki-agent开发.md`；本文件是其后续。
> 最近更新：2026-05-24

---

## 一、已完成：ReAct 过程可观测（commit f8a5751）

### 动机
WikiAgent 跑起来是黑盒——不知道它在想什么、调了哪个工具、结果如何、花了多久、烧了多少 token。

### 关键发现
`wiki_agent.py` 旧代码 `if resp.content.strip(): yield "思考：…"`，但对 deepseek-v4-pro 这类**思考模型**，带 tool_call 的那轮 `resp.content` 通常**为空**——真正的推理链在 `resp.reasoning_content` 里。代码早已捕获并回传它（思考模型约束），却从未拿来显示。这是黑盒感的头号原因。

### 改动（仅 CLI，只动 WikiAgent，不碰 messages 内容 / 工具层 / 前端）
- **思考可见**：surface `resp.reasoning_content`（回退 `content`）作思考轨迹。
- **工具调用 + 结果摘要**：每个工具调用配一条结果摘要（read=行数/字数、write=字符数、list=页面数），成功也显示（旧版只在出错时显示、成功丢弃）。`_describe_call` 拆成 `_describe_action` + `_summarize_obs`。
- **分步耗时 + token 增量**：每步 `llm.chat` 后发本步耗时 + `resp.usage.total_tokens`。采用「分步离散更新」，不做流式（工具路径设计上不流式）。
- **CLI 缩进树**：`_print_event` 按 `metadata.trace` 渲染 `✻`(思考主行) / `⏺`(工具主行) / `⎿`(结果·指标子行)；无 `trace` 的旧 log 照旧走 `›`，向后兼容。

事件约定：全部 `type="log"` + `metadata={"trace": "think"|"action"|"leaf"}`。

---

## 二、测试数据（前后调优对比基线）

**输入 fixture**：`/Users/sunqingqing/projects/personal-wiki/raw/sources/20260108.md`
- Elon Musk 2026-01-08 访谈笔记，38 行 / 1051 字。主题极发散（AI、机器人、社交媒体、模拟器假说、人口、投资、教育、太空、宗教…）。
- 选它做基线是因为：**单篇、跨多主题、与现有页面无重叠**——能同时触发「新建类别 + 候选筛选 + index 更新」全流程。

**⚠ 对比方法学注意**：`wiki/` 是 gitignore 的（非版本控制），且基线那次跑**已经把 `People/elon-musk.md` 写进了 wiki、改了 index**。要做公平的前后对比，必须先把 wiki **复位到固定的初始状态**再重跑。建议：把初始 wiki 状态（下方「基线初始 wiki」）单独存一份 fixture，每次 A/B 前还原。

---

## 三、基线指标（2026-05-24，优化前，commit f8a5751）

**基线初始 wiki**（ingest 前）：3 文件
- `index.md`
- `AI/agent-builder.md`（2232 字）
- `AI/ideal-agent-assistant.md`（1999 字）

**本次 ingest 过程**：6 次 LLM 调用 / 7 次工具调用，约 83s

| 步 | 耗时 | 本步 token | 工具调用 |
|---|---|---|---|
| 1 | 4.0s | 2091 | read_file(index.md) + list_files() |
| 2 | 26.0s | 3435 | read_file(AI/agent-builder.md) + read_file(AI/ideal-agent-assistant.md) |
| 3 | 35.0s | 7051 | write_file(People/elon-musk.md) |
| 4 | 5.6s | 7369 | read_file(index.md) ← **重复读** |
| 5 | 6.4s | 7987 | write_file(index.md) |
| 6 | 6.1s | 8194 | （无 tool_call，自然停 + 总结） |

**总 token = 36127（input=32420 / output=3707）**。单步 input 从 2k 爬到 8k。

**结果质量：合格**。正确新建 `People/elon-musk.md`（第三页、首个 People 类别），按主题拆 8 节；新建 People 类别（不硬塞进 AI）；更新 index 且不动 AI 条目。**优化目标是在不掉质量的前提下降成本。**

---

## 四、问题梳理（症状 → 根因）

成本几乎全在反复重发的输入上（input 32k vs output 3.7k）。

| # | 现象（trace 实证） | 代码 / SCHEMA 根因 |
|---|---|---|
| 1 | 单步 input 2k→8k | ReAct 的 `messages` **只增不减**，每步重发全部历史；`read_file` 把整页全文灌进 context 后**永不释放** |
| 2 | 每次都先读整篇 index | workflow #1 强制读全量 index，且 index 随页面数**无界增长** |
| 3 | 判定要建新类别后，仍读了两篇 AI 全文 | workflow #3 让 LLM「读候选全文确认相关性」，**无读取页数上限**；整篇读取让每次确认都贵 |
| 4 | index.md 被读两次（步1、步4） | 「写前必先 read」硬纪律（schema:71）是钝器——不区分「我刚读过且没变过」；它存在是因为 ↓ |
| 5 | （共因） | `write_file` **整篇覆盖**：不先读就写会毁页 → 催生 #4 的重复读；改 index 一行也要重写整篇 |

**两簇根因**：
- **(I) 整篇读/写 + context 单调累积** → 成本随 wiki 规模与步数膨胀（#1/#2/#3/#5）。**主因。**
- **(II) 相关性筛选全靠 LLM 软判断，无代码边界** → 读几页不可控（#3）、读过不去重（#4）。

### "100 篇会不会全读？"
大概率不会全读，但成本模型很糟且无硬保证：
- LLM 靠 index 的 entities/description 圈候选，只读它认为相关的几篇——但**读几篇完全由 LLM 自由判断，代码无任何上限**（workflow #3 没说最多读几篇）。
- 更关键：无论读不读候选，**每次 ingest 都必然先读整篇 index**（workflow #1）。100 篇时 index 本身就是 100 条大文件——**确定性的、随规模线性增长**的底盘成本。

---

## 四点五、成本模型修正（看过 provider 序列化后）

读 `openai_provider.py:58` 后修正：**真正的成本大头不是 index（3 页时才 678 字），而是累积的 `reasoning_content`**。
- 每个带 tool_call 的 assistant 历史轮都**原样回传 `reasoning_content`**（思考模型续接约束，`_serialize_message` 第 58 行），而 `wiki_agent.py` 每步追加一条这样的 assistant 轮。
- 即：**所有历史思考块每步都被重发一遍**。trace 里步2/步3 各是上千字长篇推理（且来回重提实体、纠结类别），它们从产生起就滚雪球。
- 每步 token = 至此累积全部 messages + 输出。累积三大块按权重：① 历史 reasoning_content（最大）② 读进来的整页正文（赖着不走）③ 每多一次 LLM 往返就把上面全部重付。

**结论**：优先级从「index 太大」重排为 **① 历史思考累积 → ② 没必要读的正文 → ③ 往返次数**。

---

## 五、优化方向与顺序（Opt3 先行）

> 排序理由（用户洞察）：先除掉最大的冗余（累积思考），后续 Opt 的真实边际收益才量得准——否则降幅被噪声淹没。

**Opt 3 — 裁剪历史 reasoning_content【❌ 已证伪，2026-05-24 已撤销】**
- 试过：每步 chat 前只保留最近一条 assistant 轮的 reasoning_content，更早的清空。
- **结果：第 3 步即 400** —— `The reasoning_content in the thinking mode must be passed back to the API.`。复盘：A1(带 tool_calls 的轮)被剥掉 reasoning 后发出即报错。
- **硬约束（记牢，勿再naively重试）**：DeepSeek thinking 模式要求**每一个带 tool_call 的 assistant 历史轮都必须携带 reasoning_content**，不只是最近一条。累积的思考是**不可移除的强制包袱**。
- **战略反转**：既然累积思考删不掉，唯一能压它的杠杆就是**减少带 tool_call 的轮数**——每多一轮，其思考永久焊进 context。这把 Opt 1 / Opt 2 从"次要"提到"主要手段"。
- 残留可选实验（高风险、未做）：把旧轮 reasoning **截断成短桩**而非清空（赌 API 只校验"存在"不校验内容）。可能省 token，但赌未文档化行为 + 模型见到残缺历史可能降质。暂不做。

**Opt 1 — 读全文只为「更新」不为「排除」【待做，纯 SCHEMA，现升为首攻】**
- workflow #3 改：相关性用 index 描述/实体判断；只有决定更新某页、需当前内容来合并时才 read 全文。干掉「读了又判无关」的纯浪费。

**Opt 2 — index 交给代码，LLM 不再读写 index【用户已认可方向，排在 Opt1 后】**
- index 是页面 frontmatter 的确定性投影：代码开局注入精简 catalog（免 read index）、ReAct 后从 frontmatter 重生成 index。页面 frontmatter 加 `description`（LLM 写页时产出）。
- 干掉步1读 + 步4冗余重读 + 步5写index 三处往返，并防 100 页 index 膨胀。revise 了「index 是 LLM 维护的脊梁」原则，理由：组装是机械活、非策展判断。

**后续**：增量编辑工具（替代整篇覆盖，去掉写前必读）；候选页读取硬上限 + grep/摘要替代读全文。

---

## 六、Opt 1 实测：行为成功，但测量被混淆（2026-05-24）

复位 wiki 后用 Musk fixture 重跑（新 SCHEMA）：

| | 基线 | Opt 1 本次 |
|---|---|---|
| 步数 | 6 | 8 |
| 新建页面 | 1（People） | **3**（AI + Media + Philosophy） |
| 总 token | 36127 | **75382** |
| 读 AI 页全文？ | 读了 2 篇 | **没读** ✓ |

- **Opt 1 行为上成功**：全程未 read_file 两篇 AI 页，凭 index 描述即判无关跳过——目标浪费消除。
- **但 token 翻倍，非 Opt 1 之过**：本次模型决定拆 3 页（前两次各 1 页），且 step 2 一步 144.7s / +8699 token 纯思考（脑内把 3 页全规划起草）。

**关键结论——真正的成本野兽**：三次跑（People 1 页 / AI 1 页 / 3 页）证明**同一文档的策展判断 run-to-run 剧烈波动**，它决定写几页、纠结多久、产多少思考；而思考不可删（Opt 3）且每步累积。**主导成本是「模型自由发挥的、海量且每次不同的思考」，不是读取/往返。** Musk 这份天生跨主题，是**最差的测量标尺**（最大化诱发纠结与拆页波动）。

---

## 七、测量基础设施（已建，git 追踪）

为了让 config 间的 token 差有意义（而非被策展波动淹没）：

- **稳定测量 fixture**：`tests/fixtures/ingest_single_topic.md`（RAG 单主题笔记，明显属 AI 类、与现有 Agent 页不重叠 → 模型基本只"在 AI 下建 1 页"，策展波动最小）。
- **基线 wiki 快照**：`tests/fixtures/wiki_baseline/`（2 篇 AI 页 + 2 条目 index，git 追踪；`wiki/` 本身 gitignore）。
- **复位脚本**：`sh tests/reset_wiki.sh`（拷贝快照 → `wiki/`，一键复位，起点可复现）。
- **Musk fixture**（`personal-wiki/raw/sources/20260108.md`）降级为**质量/压力测试**，不用于性能测量。

**测量协议**：每次 `sh tests/reset_wiki.sh` → 跑 agent on `ingest_single_topic.md` → 记总 token + 工具序列。A/B 时同一 fixture、同复位、改一个变量。单主题已压住大波动；残余思考抖动可跑 2-3 次取齐。

---

## 八、Opt 1 在稳定 fixture 上的干净 A/B（2026-05-24，已验证）

同一 RAG 单主题 fixture、同复位起点、只改 SCHEMA：

| | A（旧 SCHEMA） | B（Opt 1） |
|---|---|---|
| 步数 | 6 | **4** |
| 工具序列 | read index, list, **read 2 AI 页**, write RAG, **read index(重), write index** | read index, list, write RAG, write index |
| 读 AI 页全文？ | 读 2 篇 | **没读** ✓ |
| 总 token | 28264 | **14435（−49%）** |

- **策展结果完全一致**（都在 AI 下建 1 个 RAG 页、内容质量相同）→ 零质量损失。delta 纯来自行为变化。
- Opt 1 消除了"为排除而读全文"（步2 的 2 次读），近乎腰斩 token。
- **彩蛋**：B 还省掉了 A 的 index 冗余重读——模型自述"I already have it from the first read"。"读取要省"纪律似乎外溢，连带压住了重复读（原 #4）。
- **方法学验证**：稳定 fixture 让 delta 一眼可读（对比 Musk 那次 36127 vs 75382 完全混淆）。1+2 投资值回。

> 注：旧 SCHEMA 在单主题上也会读 2 AI 页（28264），印证浪费真实存在；Musk 那次没读纯属那轮恰好直奔拆页。

---

## 九、Opt 2 实测：index 交给代码（2026-05-24，已验证并提交）

Step 2a（`core/wiki_index.py`，commit fc6fcab）：frontmatter 解析 + catalog/index 投影 + 容错 + 回填 2 基线页 description。
Step 2b（commit 10f0fd1）：catalog 注入 prompt + 收尾 regenerate_index + SCHEMA 改（加 description 字段、删读/写 index 工作流）+ tools 硬挡 index.md 写入。

稳定 fixture 完整曲线：

| | 步数 | 工具调用 | 总 token |
|---|---|---|---|
| 优化前（旧 SCHEMA） | 6 | read index, list, **读2篇AI页**, write RAG, read index, write index | 28264 |
| Opt 1 | 4 | read index, list, write RAG, write index | 14435 |
| **Opt 2** | **2** | **只 write 新页** | **7943** |

- Opt 2 相对 Opt 1 **−45%**，相对优化前 **−72%**，零质量损失。
- 行为全部符合预期：不读 index、不 list、不写 index；LLM 理解新契约（自述"index.md 由系统自动维护，无需处理"）。
- 代码重生成的 index.md 含全部 3 条目（2 基线 + RAG），描述/实体/来源齐全，确定性、零漂移。

---

## 十、下次从这里继续
Opt 1 + Opt 2 累计把稳定 fixture 从 28264 降到 7943（−72%），可观测性 + 测量地基齐备。剩余野兽是**模型自由发挥的海量思考**（不可删，见 Opt 3）——它随任务歧义与产出页数波动，是当前主导成本。可选下一步：① 治 curation run-to-run 不一致（输出一致性，也间接控思考量/页数）；② 探"减少模型纠结/啰嗦"（偏设计、权衡质量）；③ 在 Musk 压力 fixture 上回归验证两个 Opt 的鲁棒性。
