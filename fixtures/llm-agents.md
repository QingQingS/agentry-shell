# Offline corpus: LLM agents & architectures

> 离线 demo 语料（RETRIEVER=local 时被 LocalFileRetriever 索引）。
> 每个条目是一段缓存的"论文/资料"摘要，关键词命中即返回。内容为公开常识性概述，
> 仅用于无外网环境演示检索→研究→落盘闭环，不代表任何外部 API 的真实返回。

## ReAct: Synergizing Reasoning and Acting in Language Models
ReAct prompts a large language model to interleave reasoning traces (thoughts) with task-specific actions (tool calls). The thought-action-observation loop lets the model plan, query external tools such as search APIs, observe results, and revise its plan. ReAct reduces hallucination compared to chain-of-thought alone because each action grounds the model in retrieved evidence. It is the canonical pattern behind most modern tool-using agents, including this project's ResearchAgent inner loop.

## Reflexion: Language Agents with Verbal Reinforcement Learning
Reflexion equips an LLM agent with self-reflection: after a failed attempt the agent writes a natural-language critique of what went wrong and stores it in episodic memory, then retries. This verbal reinforcement improves multi-step reasoning and decision making without updating model weights. Reflexion is complementary to ReAct and is often cited in discussions of agent robustness and failure handling.

## Toolformer and tool-use in large language models
Toolformer shows that an LLM can teach itself to call external APIs (calculator, search, translation) by deciding which tool to call, when, and with what arguments. Function calling / tool calling is now a first-class API feature across providers. Reliable tool calling — schema-constrained arguments, error observations fed back into the loop — is the contract foundation that multi-agent orchestrators depend on.

## Multi-agent orchestration: hub-and-spoke and ReAct decomposition
A hub-and-spoke architecture places a coordinator (hub) that decomposes a user task and dispatches sub-tasks to specialized worker agents (spokes) such as a research agent or a wiki curator. Rather than fixed intent routing, modern coordinators use an emergent ReAct decomposition: the hub reasons about the task, dispatches a spoke, observes a compact summary plus an artifact path, and decides the next step. Passing only summaries and artifact paths between agents — instead of full reports — keeps the hub's context small, a key context-engineering technique for long composite tasks.

## Retrieval-augmented generation (RAG) for research agents
Retrieval-augmented generation grounds an LLM's output in documents fetched from an external or local corpus. A research agent decomposes a broad question into sub-questions, retrieves evidence per sub-question from sources such as arXiv or a local file index, summarizes each, and synthesizes a structured report. Keeping a cached local corpus enables fully offline, reproducible demos that do not depend on flaky external search services.
