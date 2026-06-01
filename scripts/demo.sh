#!/usr/bin/env bash
#
# 60 秒离线 demo —— 检索层完全不触外网（RETRIEVER=local 读 fixtures/ 缓存语料）。
#
# 前置：仅需一个 LLM API key（OPENAI_API_KEY 或对应 provider 的 key），用于驱动
#       ResearchAgent 的 ReAct 循环；检索不依赖 arxiv/tavily 等外部服务。
#
# 用法：  bash scripts/demo.sh
#         DEMO_TASK="你的任务" bash scripts/demo.sh   # 自定义任务
#
set -euo pipefail
cd "$(dirname "$0")/.."

DEMO_TASK="${DEMO_TASK:-Survey recent progress on LLM agent architectures: ReAct, Reflexion, tool use, and multi-agent hub-and-spoke orchestration.}"

echo "▶ 离线检索模式：RETRIEVER=local（读 fixtures/，不触外网）"
echo "▶ 任务：$DEMO_TASK"
echo

RETRIEVER=local python cli.py "$DEMO_TASK" --agent agents.research_agent.ResearchAgent

echo
echo "✅ demo 完成。报告已落盘到 reports/（见上方 save_report 输出）。"
