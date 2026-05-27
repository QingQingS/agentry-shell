# Agentry-Shell

A from-scratch, extensible **LLM agent runtime**. Not a framework wrapper — every layer
(agent lifecycle, event protocol, intent-driven orchestration, ReAct tool loop, streaming
transport, pluggable LLM providers and retrievers) is built and owned here, to understand how
a real agent system fits together.

The `-shell` is deliberate: the project is a *substrate* that runs agents, not a single agent.
Drop in a new agent class, an LLM provider, or a retriever — the infrastructure layer doesn't change.

The architecture began as a study of [gpt-researcher](https://github.com/assafelovic/gpt-researcher) —
its retriever/agent layering was the starting reference — and then grew its own agent runtime,
event protocol, intent-driven orchestrator, and ReAct tool loop. Nothing is imported as a black box;
every layer is reimplemented to understand it.

```
User (CLI / Web UI / REST)
        │
        ▼
   run_agent()                      ← unified lifecycle + status/error wrapper (core/runner.py)
        │                             agents only yield domain events & raise on failure
        ▼
  OrchestratorAgent                 ← the conversation brain (AGENT_CLASS)
   ├─ SessionManager                ← cross-turn memory: turns + a rolling report window
   ├─ classify_intent (fast LLM)    ← one call → {route, mode, target, carry_context, files}
   └─ dispatch by route:
        ├─ research → ResearchAgent  ← multi-source retrieval + streamed report
        ├─ chat     → ChatAgent      ← answers from carried-over context, no new retrieval
        └─ wiki     → WikiAgent      ← ReAct tool loop, curates a persistent ./wiki/ knowledge base
```

## Highlights

- **Agent runtime / harness.** A single `AgentInterface` contract + `run_agent()` driver own the
  whole lifecycle (`on_start` → `running` → `done` / `error`). Agents stay simple: implement one
  async generator, `yield` domain events, raise on failure. Status/error emission is never the
  agent's job.
- **Uniform event protocol.** Every agent speaks `AgentEvent` (`log` / `stream` / `result` /
  `status` / `tokens`). The same stream drives the CLI, the WebSocket, and the Web UI — transports
  are decoupled from agents.
- **Intent-driven orchestration.** `OrchestratorAgent` turns a series of stateless workers into a
  coherent multi-turn conversation. One fast-LLM call classifies each input along orthogonal axes
  (`route` / `mode` / `target` / `carry_context` / `files`) and routes it. Pronoun resolution and
  follow-ups work because the orchestrator carries prior reports as background.
- **A real ReAct agent.** `WikiAgent` is genuinely agentic: an LLM tool-calling loop that decides
  which wiki pages to read/write and how to update the index, integrating documents into a
  topic-centric knowledge base. Tools run in a path-sandboxed registry (`./wiki/` only, blocks
  traversal / absolute / symlink escapes), tools never raise (failures become observations), and the
  loop has loop-breaking guards (duplicate-call nudge, `MAX_STEPS` cap).
- **Pluggable everywhere.** LLM providers (OpenAI / DeepSeek; Anthropic text path) behind a
  `factory` with `smart` / `fast` tiers; retrievers (arXiv / Tavily / local files) behind a
  `BaseRetriever`; agents loaded dynamically via `AGENT_CLASS`. Adding any of these touches no core code.
- **Streaming first.** Token-level streaming over WebSocket, with per-session token accounting.

## Optimizing the ReAct loop, measured

`WikiAgent`'s tool loop was profiled and tuned with a measurement-first workflow: first an
event-level trace (per-step reasoning, tool calls, timing, token deltas), then a fixed
single-topic ingestion fixture so runs are comparable. Ingesting one document into a two-page
wiki on that fixture:

| Version | LLM round-trips | Tool calls | Total tokens |
|---|---|---|---|
| Baseline | 6 | read index · list · **read 2 unrelated pages** · write page · re-read index · write index | 28,264 |
| + relevance judged from the index, read-to-update only | 4 | read index · list · write page · write index | 14,435 |
| + code owns the index (LLM never reads/writes it) | **2** | write page | **7,943** |

**−72% tokens, identical curation output.** Two findings shaped the work:

- *Build the instrument first.* Single-run token counts on an ambiguous, multi-topic document were
  dominated by curation variance (the model created 1 vs 3 pages run-to-run), swamping the signal.
  A fixed single-topic fixture (`tests/fixtures/`, with a git-tracked baseline wiki and a one-command
  reset) made each change's effect legible.
- *A falsified hypothesis redirected the effort.* Pruning the model's accumulated `reasoning_content`
  looked like the largest lever — but the thinking-mode API rejects any tool-call turn that drops it,
  so the reasoning can't be removed from history. The effort shifted to removing the *turns* that
  accumulate it: judging relevance from the index instead of reading pages, and pushing index
  maintenance out of the loop into deterministic code.

## Quick start

Requires Python ≥ 3.11.

```bash
git clone git@github.com:QingQingS/agentry-shell.git
cd agentry-shell

python -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env      # then fill in your LLM API key
```

Minimal `.env` (DeepSeek shown; OpenAI works the same way):

```bash
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-...
SMART_LLM_MODEL=deepseek-chat
FAST_LLM_MODEL=deepseek-chat
AGENT_CLASS=agents.orchestrator_agent.OrchestratorAgent
RETRIEVER=arxiv                 # or: tavily / arxiv,tavily (Tavily needs TAVILY_API_KEY)
```

Run it:

```bash
# One-shot task
python cli.py "Survey recent work on speculative decoding"

# Multi-turn conversation (session memory across turns)
python cli.py --interactive

# Web service → http://localhost:8000  (FastAPI + WebSocket + minimal UI)
python main.py
```

In `--interactive` mode the orchestrator remembers the conversation, so you can follow up
("what did that report mean by X?"), pivot ("forget that, research Y"), or archive
("save ./reports/foo.md into the wiki") and watch it route to the right agent.

## Layout

```
core/
  agent_interface.py   # AgentInterface + AgentEvent — the contract every agent implements
  runner.py            # run_agent(): lifecycle + status/error, owned in one place
  session.py           # SessionManager: turns + rolling report window, persisted to ./reports/
  intent.py            # classify_intent → {route, mode, target, carry_context, files}
  tools.py             # Tool / ToolRegistry + sandboxed file tools for the wiki
  config.py            # config resolution: env > .env > defaults
  llm/                 # BaseLLM, OpenAI/DeepSeek/Anthropic providers, factory, tool-calling path
  retrievers/          # BaseRetriever + arXiv / Tavily / local-file sources
agents/
  orchestrator_agent.py  # continuous-conversation orchestrator (the brain)
  research_agent.py      # multi-source concurrent retrieval + streamed report (survey/paper/code modes)
  chat_agent.py          # context-aware single-turn chat
  wiki_agent.py          # ReAct tool loop — persistent knowledge curation
  echo_agent.py          # zero-LLM reference agent
backend/server/          # FastAPI routes + WebSocket manager
frontend/                # minimal HTML / JS / CSS client
cli.py  main.py          # CLI and web entry points
```

## Extending it

**Add an agent** — implement one method, point `AGENT_CLASS` at it, done:

```python
from core.agent_interface import AgentInterface, AgentEvent

class MyAgent(AgentInterface):
    async def run(self, task: str, **kwargs):
        yield AgentEvent(type="log", content="working...")
        result = await do_something(task)     # just raise on failure; run_agent() handles it
        yield AgentEvent(type="result", content=result)
```

```bash
AGENT_CLASS=agents.my_agent.MyAgent python cli.py "..."
```

To teach the orchestrator a new capability, add a `route` arm in `core/intent.py` (a route value,
its payload, a few classification examples) and a dispatch branch in `orchestrator_agent.py` —
the `research` / `chat` / `wiki` paths stay untouched. New LLM providers and retrievers slot in
behind `core/llm/factory.py` and `BaseRetriever` the same way.

## Status & roadmap

Actively developed as a learning project. The agent runtime, multi-source research,
continuous-conversation orchestration, and the ReAct WikiAgent are all implemented and runnable
end-to-end against a real LLM.

In progress toward review-grade quality: folding the existing `tests/check_*.py` verification
scripts into a one-command `pytest` suite, tightening a couple of known rough edges (agent
auto-import, the synchronous `POST /api/run` path), and removing dead code.

Planned next: long-term memory / storage, and letting `ChatAgent` query the curated wiki.

## Tech

Python · asyncio · FastAPI · WebSocket · Pydantic · OpenAI / DeepSeek / Anthropic SDKs · arXiv · Tavily

## Acknowledgements

The initial framework was built by studying and reproducing
[gpt-researcher](https://github.com/assafelovic/gpt-researcher) by Assaf Elovic. Its layered
retriever/agent design shaped the early scaffold; the runtime, orchestration, and ReAct curation
layers here are an independent reimplementation built to learn how each piece works.
