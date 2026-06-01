"""
Anthropic Claude Provider。

与 OpenAI 协议的两个关键差异：
  1. system 提示在顶层参数，不在 messages 列表
  2. max_tokens 是必填项
"""

from __future__ import annotations

from typing import Any, AsyncIterator, List, Optional

from anthropic import AsyncAnthropic

from .base import BaseLLM, ChatMessage, LLMResponse, ToolSpec, TokenUsage


class AnthropicProvider(BaseLLM):
    provider_name = "anthropic"
    DEFAULT_MAX_TOKENS = 4096

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._client = AsyncAnthropic(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    async def _chat_impl(
        self,
        messages: List[ChatMessage],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[ToolSpec]] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        # 工具路径本期只实现 OpenAI/DeepSeek；Anthropic 的 tool_use/tool_result
        # 块往返留作扩展点（详见 wiki-agent开发.md 第七节）。
        if tools:
            raise NotImplementedError(
                "AnthropicProvider 暂不支持 tool calling；本期工具路径仅 DeepSeek/OpenAI"
            )
        system_parts = [m.content for m in messages if m.role == "system"]
        chat_messages = [m.to_dict() for m in messages if m.role != "system"]
        system = "\n\n".join(system_parts) if system_parts else None

        call_kwargs: dict = {
            "model": self.model,
            "messages": chat_messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": (
                max_tokens if max_tokens is not None
                else (self.max_tokens or self.DEFAULT_MAX_TOKENS)
            ),
        }
        if system:
            call_kwargs["system"] = system
        call_kwargs.update(kwargs)

        resp = await self._client.messages.create(**call_kwargs)

        usage = TokenUsage(
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )
        content = "".join(
            block.text for block in resp.content if hasattr(block, "text")
        )
        await self._record_usage(usage)
        return LLMResponse(
            content=content,
            usage=usage,
            model=self.model,
            provider=self.provider_name,
            raw=resp,
        )

    async def chat_stream(
        self,
        messages: List[ChatMessage],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        system_parts = [m.content for m in messages if m.role == "system"]
        chat_messages = [m.to_dict() for m in messages if m.role != "system"]
        system = "\n\n".join(system_parts) if system_parts else None

        call_kwargs: dict = {
            "model": self.model,
            "messages": chat_messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": (
                max_tokens if max_tokens is not None
                else (self.max_tokens or self.DEFAULT_MAX_TOKENS)
            ),
        }
        if system:
            call_kwargs["system"] = system
        call_kwargs.update(kwargs)

        async with self._client.messages.stream(**call_kwargs) as stream:
            async for text in stream.text_stream:
                yield text
            final_msg = await stream.get_final_message()
            usage = TokenUsage(
                input_tokens=final_msg.usage.input_tokens,
                output_tokens=final_msg.usage.output_tokens,
            )
        await self._record_usage(usage)
