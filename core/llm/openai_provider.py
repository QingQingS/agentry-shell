"""
OpenAI Provider 及其兼容协议的子类（DeepSeek 等）。

DeepSeek API 与 OpenAI 接口完全兼容，只需更换 base_url。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, List, Optional

from openai import AsyncOpenAI

from .base import BaseLLM, ChatMessage, LLMResponse, ToolCall, ToolSpec, TokenUsage


def _tool_specs_to_openai(tools: List[ToolSpec]) -> list:
    """中性 ToolSpec → OpenAI function-calling 格式。"""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


def _serialize_message(m: ChatMessage) -> dict:
    """
    ChatMessage → OpenAI wire 格式。

    纯文本走 role/content；assistant 发起工具调用时挂 tool_calls；
    tool 结果轮用 {role:"tool", tool_call_id, content}。
    """
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
    if m.tool_calls:
        msg: dict = {
            "role": m.role,
            "content": m.content or None,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in m.tool_calls
            ],
        }
        # 思考模型要求把 reasoning_content 原样回传（如 DeepSeek 思考态）
        if m.reasoning_content is not None:
            msg["reasoning_content"] = m.reasoning_content
        return msg
    return {"role": m.role, "content": m.content}


def _parse_tool_calls(message: Any) -> List[ToolCall]:
    """从 OpenAI 响应 message 解析工具调用；参数 JSON 解析失败降级为 {}（可恢复）。"""
    raw_calls = getattr(message, "tool_calls", None)
    if not raw_calls:
        return []
    parsed: List[ToolCall] = []
    for tc in raw_calls:
        try:
            args = json.loads(tc.function.arguments)
            if not isinstance(args, dict):
                args = {}
        except (json.JSONDecodeError, TypeError):
            args = {}
        parsed.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
    return parsed


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
        tools: Optional[List[ToolSpec]] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        if tools:
            kwargs["tools"] = _tool_specs_to_openai(tools)
        resp = await self._client.chat.completions.create(
            model=self.model,
            messages=[_serialize_message(m) for m in messages],
            temperature=temperature if temperature is not None else self.temperature,
            max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
            **kwargs,
        )
        usage = TokenUsage(
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
        )
        message = resp.choices[0].message
        tool_calls = _parse_tool_calls(message)
        stop_reason = "tool_calls" if tool_calls else "stop"
        await self._record_usage(usage)
        return LLMResponse(
            content=message.content or "",
            usage=usage,
            model=self.model,
            provider=self.provider_name,
            raw=resp,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            reasoning_content=getattr(message, "reasoning_content", None),
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
