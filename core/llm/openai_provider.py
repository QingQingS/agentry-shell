"""
OpenAI Provider 及其兼容协议的子类（DeepSeek 等）。

DeepSeek API 与 OpenAI 接口完全兼容，只需更换 base_url。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, List, Optional

from openai import AsyncOpenAI

from .base import BaseLLM, ChatMessage, LLMResponse, TokenUsage


class OpenAIProvider(BaseLLM):
    provider_name = "openai"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    async def chat(
        self,
        messages: List[ChatMessage],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        resp = await self._client.chat.completions.create(
            model=self.model,
            messages=[m.to_dict() for m in messages],
            temperature=temperature if temperature is not None else self.temperature,
            max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
            **kwargs,
        )
        usage = TokenUsage(
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
        )
        content = resp.choices[0].message.content or ""
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
        stream = await self._client.chat.completions.create(
            model=self.model,
            messages=[m.to_dict() for m in messages],
            temperature=temperature if temperature is not None else self.temperature,
            max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
            stream=True,
            stream_options={"include_usage": True},
            **kwargs,
        )
        usage = TokenUsage()
        async for chunk in stream:
            if chunk.usage:
                usage = TokenUsage(
                    input_tokens=chunk.usage.prompt_tokens,
                    output_tokens=chunk.usage.completion_tokens,
                )
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
        await self._record_usage(usage)


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek 复用 OpenAI SDK，base_url 不同。"""

    provider_name = "deepseek"
    DEFAULT_BASE_URL = "https://api.deepseek.com"

    def __init__(self, *args: Any, **kwargs: Any):
        if kwargs.get("base_url") is None:
            kwargs["base_url"] = self.DEFAULT_BASE_URL
        super().__init__(*args, **kwargs)
