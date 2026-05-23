"""
ChatAgent —— 单轮聊天 Agent。

最小可用 Agent，验证：
  1. core/llm 抽象层能正常调通真实 LLM Provider（OpenAI / DeepSeek / Anthropic）
  2. tokens 事件在 CLI / WebSocket 端到端透传
  3. AgentEvent 协议与 LLM 抽象层的衔接方式

替换为多轮 / 研究 Agent 时，只需复用相同的 yield + chat() 模式。
"""

from __future__ import annotations

from typing import AsyncIterator

from core.agent_interface import AgentEvent, AgentInterface
from core.llm import ChatMessage, get_llm


class ChatAgent(AgentInterface):
    name = "ChatAgent"
    description = "单轮聊天，调用 LLM 返回回答。"

    SYSTEM_PROMPT = "你是一个简洁、准确的中文助理，回答控制在 200 字以内。"

    def _system_with_context(self, context: str = "") -> str:
        """有 context（上轮研究背景）时拼进 system prompt，供追问参考。"""
        if context:
            return (
                self.SYSTEM_PROMPT
                + "\n\n以下是之前的研究背景，回答用户追问时请参考它：\n"
                + context
            )
        return self.SYSTEM_PROMPT

    async def run(self, task: str, **kwargs) -> AsyncIterator[AgentEvent]:
        # 生命周期与异常→error 事件由 core.runner 统一负责；这里只 yield 领域事件、失败时抛异常。
        context = (kwargs.get("context") or "").strip()
        llm = get_llm(tier="smart", config=self.config)
        yield AgentEvent(
            type="log",
            content=f"使用 {llm.provider_name} / {llm.model}" + ("（含研究背景）" if context else ""),
            metadata={"provider": llm.provider_name, "model": llm.model},
        )

        resp = await llm.chat(
            [
                ChatMessage(role="system", content=self._system_with_context(context)),
                ChatMessage(role="user", content=task),
            ]
        )

        yield AgentEvent(
            type="tokens",
            content=(
                f"input={resp.usage.input_tokens}  "
                f"output={resp.usage.output_tokens}  "
                f"total={resp.usage.total_tokens}"
            ),
            metadata={
                **resp.usage.to_dict(),
                "provider": resp.provider,
                "model": resp.model,
            },
        )

        yield AgentEvent(type="result", content=resp.content)
