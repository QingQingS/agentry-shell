"""
ResearchAgent —— 研究型 spoke：内部 ReAct 循环，LLM 自决检索 / 抓单页 / 广度调研。

设计（步5.5 后）：
  - 对外契约（步5 锁定）不变：入参 (prompt, context)；出 result.content（完整 markdown 报告）
    + metadata{status, summary}。
  - 内部 ReAct 循环：smart LLM 驱动，工具表见 agents/research_tools.py
    （search_papers / search_web / fetch_url / do_broad_survey）。
  - 不再有 mode/route 分发——「调研最新进展」/「读这篇 paper」/「找开源仓库」
    都由 LLM 看 prompt 自决用哪个工具。导师/学生类比。
  - status 三态：循环代码侧统计——所有工具调用 observation 都是「(...)」或「Error:」开头时
    标 degenerate；否则 ok。summary 取最终答复 markdown 的冒头段（_first_paragraph）。
  - 自然停止 = LLM 不发 tool_call；触顶 MAX_STEPS 时把当时已有 content 兜成结果。

回环兜底（复用 WikiAgent 风格）：
  - 错误转 observation（ToolRegistry.execute 永不向循环抛）。
  - 同一 (name, args) 重复 ≥ DUP_THRESHOLD 次时注入一次 nudge。
"""

from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path
from typing import AsyncIterator, List, Optional

from agents.research_tools import SaveReportTool, build_research_registry
from core.agent_interface import AgentEvent, AgentInterface
from core.llm import ChatMessage, get_llm
from core.retrievers import ArxivRetriever, BaseRetriever, LocalFileRetriever, TavilyRetriever

MAX_STEPS = 12          # 单次研究的 LLM 调用上限
DUP_THRESHOLD = 3       # 同一 (tool, args) 重复多少次触发 nudge
LOCAL_FIXTURES_DIR = "fixtures"   # RETRIEVER=local 时离线检索的语料目录

NUDGE_TEXT = (
    "提醒：你已多次执行同一个工具调用并得到相同结果，似乎卡住了。"
    "先停下来判断你属于以下哪种情况，再行动：\n"
    "1) 还有没试过的路子 —— 换工具、换关键词、放宽或收窄检索、换信息源，"
    "或把问题拆成更小的子问题分别查。不要重复同一个调用。\n"
    "2) 现有信息已确实足够回答 —— 不要再调用任何工具，直接用 markdown 写出最终报告。\n"
    "3) 已经试过多个不同思路、仍拿不到足够信息 —— 也不要再空转或反复重试。"
    "直接用 markdown 写报告，如实标注这是不完整的结果：写清你查到了什么、"
    "哪些没查到、为什么没查到（如来源不可达 / 需要登录 / 根本不存在 / 信息相互矛盾）、"
    "试过哪些途径，以及若要继续接手、下一步可往哪个方向查。\n"
    "   特例：如果连一条有用信息都没查到，那“什么都没找到”本身就是结论——"
    "明确写出“未能找到任何相关信息”，列出试过且都失败的途径与可能原因"
    "（很可能该主题不存在、检索不到、web问题或任务前提本身有误），而不是把报告硬填满。\n"
    "无论哪种情况，都绝不要为了填补空缺而编造或臆测未经证实的内容——查不到就如实说查不到。"
    "诚实的报告（哪怕只有部分、甚至完全空手），都优于硬凑，也优于无休止的重试。"
)

SYSTEM_PROMPT_TEMPLATE = """你是研究助理。手头有这些工具可用：

- search_papers(query, max_results): 检索 ArXiv 论文，query 用英文效果更好。返回标题/作者/时间/摘要/链接。
- search_web(query, max_results): 检索网页（开源仓库、博客、代码示例等）。返回标题/链接/摘要。
- fetch_url(url, max_chars): 抓取指定 URL 的文本内容（HTML 去 tag 后返回）。用于读特定论文 / 看某个 README。
- do_broad_survey(topic, background): 对一个主题做广度调研（拆问 → 多源并发检索 → 综合），返回完整 markdown 报告。
  适合「调研 X 最新进展」这类宽问题——一次调用拿到结构化产出。
- save_report(filename, content): 把最终 markdown 报告落盘到 reports/<filename>。filename 要含主题
  slug（如 rl-survey.md / vllm-analysis.md），便于下游识别。

工作流程：
1. 看用户的研究任务，自主决定调用哪些检索工具。
   - 宽调研 → 通常一次 do_broad_survey 就够（也可以再补充 search/fetch 修补具体点）。
   - 找开源实现 → search_web 优先。
   - 读特定论文 / 看 README → fetch_url 抓全文。
   - 自己拆细问题分别查 → 多次 search_papers。
2. 写最终 markdown 报告：开头一段总览，分点论述，结论收尾。
3. **只有当报告确已写完时，才调 save_report(filename, content=完整报告)。**
   调 save_report 等于你声明"这是一份完整成品"——Coordinator 会据此把产物转交下游。
   （你确实写完却漏调时系统会兜底落盘，但那是安全网，别依赖。）
   save_report 调完后下一轮直接结束：不再调任何工具，text 简短确认即可。
4. 以下两种情况，不要调 save_report、也不要硬凑一份报告：
   - 多方尝试后确实查不到任何有用信息：直接简短说明"未找到相关信息"及试过哪些途径，
     不写空壳报告。
   - 收到"接近步数/资源上限"的提醒、或自知写不完时：停止检索，把已查到的要点和
     "哪些没做完"直接写进回复文本（不必 save_report），让系统据实标记为未完成。
   无论哪种，都绝不为了凑成完整报告而编造未经证实的内容。
今天的日期：{today}
"""


class ResearchAgent(AgentInterface):
    name = "ResearchAgent"
    description = "研究型 spoke：内部 ReAct 循环，自决检索论文/网页/抓单页/广度调研。"

    async def run(self, task: str, **kwargs) -> AsyncIterator[AgentEvent]:
        # 生命周期与异常→error 由 core.runner 统一负责；这里只 yield 领域事件、失败时抛异常。
        # context 是新接口；旧 v1 OrchestratorAgent 仍传 background_context（cutover 时删）。
        background = (kwargs.get("context") or kwargs.get("background_context") or "").strip()
        reports_root = Path(kwargs.get("reports_root") or SaveReportTool.DEFAULT_ROOT)

        fast = get_llm(tier="fast", config=self.config)
        smart = get_llm(tier="smart", config=self.config)
        retrievers = self._make_retrievers()
        registry = build_research_registry(fast, smart, retrievers, reports_root=reports_root)
        specs = registry.specs()

        yield AgentEvent(
            type="log",
            content=(
                f"smart={smart.model} 驱动循环；fast={fast.model} 备 do_broad_survey；"
                f"检索源={'+'.join(r.source_name for r in retrievers)}"
            ),
            metadata={"smart_model": smart.model, "fast_model": fast.model},
        )
        if background:
            yield AgentEvent(type="log", content="（携带上轮报告作背景）")

        today = date.today().isoformat()
        user_msg = task if not background else (
            f"{task}\n\n参考上一轮研究的已有结论（承接但不重复）：\n{background}"
        )
        messages: List[ChatMessage] = [
            ChatMessage(role="system", content=SYSTEM_PROMPT_TEMPLATE.format(today=today)),
            ChatMessage(role="user", content=user_msg),
        ]

        call_counts: dict = {}
        nudged = False
        retrieval_calls = 0      # 工具调用总数（不含 save_report）
        retrieval_hits = 0       # 非空（非 Error/非「(...)」开头）的检索结果数
        final_content = ""
        stopped_naturally = False
        artifact_path: Optional[str] = None   # LLM 调 save_report 时由代码从 obs 解析

        for step in range(MAX_STEPS):
            t0 = time.monotonic()
            resp = await smart.chat(messages, tools=specs)
            dt = time.monotonic() - t0
            messages.append(ChatMessage(
                role="assistant",
                content=resp.content,
                tool_calls=resp.tool_calls,
                reasoning_content=resp.reasoning_content,
            ))

            think = (resp.reasoning_content or resp.content or "").strip()
            if think:
                yield AgentEvent(type="log", content=think, metadata={"trace": "think"})
            yield AgentEvent(
                type="log",
                content=f"第 {step + 1} 步 · {dt:.1f}s · +{resp.usage.total_tokens} tokens",
                metadata={"trace": "leaf"},
            )

            if not resp.tool_calls:
                # save_report 已提供过权威 content 则不覆盖；否则用本轮 text 兜底
                if not final_content:
                    final_content = resp.content
                stopped_naturally = True
                break

            for call in resp.tool_calls:
                # sig = (call.name, json.dumps(call.arguments, sort_keys=True, ensure_ascii=False))
                sig = (call.name,)
                call_counts[sig] = call_counts.get(sig, 0) + 1
                obs = await registry.execute(call)
                messages.append(ChatMessage(role="tool", content=obs, tool_call_id=call.id))
                yield AgentEvent(
                    type="log",
                    content=self._describe_action(call.name, call.arguments),
                    metadata={"trace": "action"},
                )
                yield AgentEvent(
                    type="log",
                    content=self._summarize_obs(call.name, obs),
                    metadata={"trace": "leaf"},
                )
                if call.name == "save_report":
                    # save_report 不算检索；从 obs 解析实际写入路径
                    # （LLM 多次调用时取最后一次成功的；其 content 是权威报告内容）
                    parsed = SaveReportTool.parse_obs_path(obs)
                    if parsed:
                        artifact_path = parsed
                        content_arg = call.arguments.get("content", "")
                        if content_arg:
                            final_content = content_arg
                else:
                    retrieval_calls += 1
                    if not self._is_empty_obs(obs):
                        retrieval_hits += 1

            if not nudged and any(c >= DUP_THRESHOLD for c in call_counts.values()):
                messages.append(ChatMessage(role="user", content=NUDGE_TEXT))
                yield AgentEvent(type="log", content="检测到重复调用，已注入提醒（nudge）")
                nudged = True

        if not stopped_naturally:
            yield AgentEvent(type="log", content=f"达到步数上限（{MAX_STEPS}），提前结束。")
            if not final_content:
                final_content = "（因达步数上限未能写出完整报告。）"

        # status 判定提前到兜底落盘之前：调过工具且全部空 → degenerate（检索无结果）。
        # 无工具调用（纯对话答复）也算 ok。
        is_degenerate = retrieval_calls > 0 and retrieval_hits == 0

        # 落盘兜底：仅当循环自然结束（LLM 主动停）却漏调 save_report 时，由代码强制落盘
        # final_content，保证跨 agent 的 artifact 契约不被 LLM 单方面打破。
        # 不兜底的两种情况：
        #   - 触顶提前结束（not stopped_naturally）：研究没做完、没产出完整报告，落盘只会把
        #     占位/半成品写进 reports/，据实留作未完成、不产 artifact。
        #   - degenerate（检索全空）：否则会把"未找到"的空壳报告写进 reports/，再经
        #     stage_files 泄漏进 wiki（与 T6 同向，保持产物干净）。
        if stopped_naturally and not is_degenerate and artifact_path is None and final_content.strip():
            artifact_path = self._fallback_save(reports_root, task, final_content)
            yield AgentEvent(
                type="log",
                content=f"LLM 未调 save_report，代码兜底落盘 → {artifact_path}",
            )

        total = fast.cumulative_usage + smart.cumulative_usage
        yield AgentEvent(
            type="tokens",
            content=f"累计 input={total.input_tokens} output={total.output_tokens} total={total.total_tokens}",
            metadata={**total.to_dict(), "scope": "cumulative"},
        )

        if is_degenerate:
            status, summary = "degenerate", "（未检索到相关结果）"
        elif not stopped_naturally:
            # 触顶提前结束：研究没做完、未产出完整报告（上面已跳过兜底落盘，无 artifact）。
            # 据实标记为未完成，让 Coordinator 知道这不是一份成品、勿当成功转交下游。
            status, summary = "incomplete", "（达步数上限，研究未完成）"
        else:
            status, summary = "ok", self._first_paragraph(final_content)
        result_meta = {"status": status, "summary": summary}
        if artifact_path:
            result_meta["artifact_path"] = artifact_path
        yield AgentEvent(
            type="result",
            content=final_content,
            metadata=result_meta,
        )

    # ---- 辅助 ----

    def _make_retrievers(self) -> List[BaseRetriever]:
        """解析 config.retriever（逗号分隔），返回 retriever 列表。

        测试通过 monkey-patch 这个方法注入 fake retrievers。"""
        names = [
            n.strip().lower()
            for n in getattr(self.config, "retriever", "arxiv").split(",")
            if n.strip()
        ]
        retrievers: List[BaseRetriever] = []
        for name in names:
            if name == "tavily":
                retrievers.append(
                    TavilyRetriever(api_key=getattr(self.config, "tavily_api_key", None))
                )
            elif name == "local":
                # 离线模式：从本地语料目录（fixtures/）做关键词检索，不触任何外网。
                retrievers.append(LocalFileRetriever(LOCAL_FIXTURES_DIR))
            else:
                retrievers.append(ArxivRetriever())
        return retrievers or [ArxivRetriever()]

    @staticmethod
    def _is_empty_obs(obs: str) -> bool:
        """工具观察是否「无收获」：以 `(` 开头（约定的占位）或 `Error:` 开头。"""
        s = obs.lstrip()
        return s.startswith("(") or s.startswith("Error:")

    @staticmethod
    def _describe_action(name: str, args: dict) -> str:
        if name in ("search_papers", "search_web"):
            return f"{name}({args.get('query')!r})"
        if name == "fetch_url":
            return f"fetch_url({args.get('url')})"
        if name == "do_broad_survey":
            return f"do_broad_survey({args.get('topic')!r})"
        if name == "save_report":
            return f"save_report({args.get('filename')!r})"
        return f"{name}({json.dumps(args, ensure_ascii=False)})"

    @staticmethod
    def _summarize_obs(name: str, obs: str) -> str:
        if obs.startswith("Error:"):
            return f"✗ {obs}"
        if obs.startswith("("):
            return obs
        if name == "do_broad_survey":
            return f"得到 broad_survey 报告 / {len(obs)} 字"
        if name == "fetch_url":
            return f"抓到 {len(obs)} 字"
        if name == "save_report":
            return obs   # 已是 "已保存 reports/xxx.md（n 字符）"
        # search_papers / search_web：用 `\n\n` 计条数
        n = obs.count("\n\n") + 1
        return f"得到 {n} 条结果 / {len(obs)} 字"

    @staticmethod
    def _fallback_save(reports_root: Path, task: str, content: str) -> str:
        """LLM 漏调 save_report 时的兜底落盘。

        文件名生成：task 前 30 字符的 slug + 时间戳后缀，避免与 LLM 主动落盘冲突。
        路径返回相对 cwd（方便 Coordinator 后续 stage_files 引用）。
        """
        slug = re.sub(r"[^\w\-]+", "-", task[:30]).strip("-").lower() or "report"
        ts = time.strftime("%Y%m%d-%H%M%S")
        root = Path(reports_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"auto-{slug}-{ts}.md"
        target.write_text(content, encoding="utf-8")
        try:
            return str(target.relative_to(Path.cwd()))
        except ValueError:
            return str(target)

    @staticmethod
    def _first_paragraph(text: str) -> str:
        """报告冒头一段：跳过空行和 # 标题行，取第一段连续内容；找不到则回退取前 200 字。"""
        para: List[str] = []
        for ln in text.strip().splitlines():
            s = ln.strip()
            if not s:
                if para:
                    break
                continue
            if s.startswith("#"):
                if para:
                    break
                continue
            para.append(s)
        if para:
            return " ".join(para)
        return re.sub(r"\s+", " ", text).strip()[:200]
