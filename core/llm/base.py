"""
LLM 抽象层基础接口。

设计原则：
  - 上层 Agent 调用 llm.chat(messages) 即可，不感知具体 Provider
  - 每个 Provider 子类实现 chat() 异步方法，返回 LLMResponse(content, usage)
  - Token 计数由各 Provider 从 API 响应中提取，归一化为 TokenUsage
  - 通过 on_tokens 回调把每次调用的 usage 转发给上层（如 Agent → yield event）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, List, Literal, Optional


Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolSpec:
    """喂给 LLM 的工具定义，与具体 Provider 无关。"""

    name: str
    description: str
    parameters: dict  # JSON Schema，描述该工具的入参


@dataclass
class ToolCall:
    """LLM 要求调用某工具时的归一化表示；id 用于回填工具结果时对齐。"""

    id: str           # provider 给的 call id
    name: str
    arguments: dict   # 已解析为 dict（OpenAI 原为 JSON 字符串，在 provider 层解析）


@dataclass
class ChatMessage:
    role: Role
    content: str
    tool_calls: Optional[List[ToolCall]] = None   # assistant 轮：LLM 发起的工具调用列表
    tool_call_id: Optional[str] = None            # tool 结果轮：对应的 call id

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class LLMResponse:
    content: str
    usage: TokenUsage
    model: str
    provider: str
    raw: Optional[Any] = None                                  # 原始响应对象，调试用
    tool_calls: List[ToolCall] = field(default_factory=list)  # 无工具调用时为空列表
    stop_reason: str = "stop"                                  # 归一化停止原因："stop" | "tool_calls"


# Token 回调签名：(usage, provider, model) → None 或 Awaitable[None]
TokenCallback = Callable[[TokenUsage, str, str], Optional[Awaitable[None]]]


class BaseLLM(ABC):
    """所有 LLM Provider 的抽象基类。"""

    provider_name: str = "base"

    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        base_url: Optional[str] = None,
        on_tokens: Optional[TokenCallback] = None,
    ):
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.base_url = base_url
        self.on_tokens = on_tokens
        self._cumulative_usage = TokenUsage()

    @abstractmethod
    async def chat(
        self,
        messages: List[ChatMessage],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """
        非流式调用。等待完整响应，返回 LLMResponse。

        子类实现要点：
            1. 调用 SDK
            2. 从响应提取 usage，构造 TokenUsage
            3. await self._record_usage(usage) 触发回调与累计
            4. 返回 LLMResponse
        """
        ...

    async def chat_stream(
        self,
        messages: List[ChatMessage],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """
        流式调用，逐块 yield 文本增量。流结束后触发 on_tokens 回调并累计用量。

        默认实现：调用 chat() 一次性返回完整内容（单块输出，无真实流式效果）。
        Provider 子类覆盖此方法以实现逐 token 推送。
        """
        resp = await self.chat(
            messages, temperature=temperature, max_tokens=max_tokens, **kwargs
        )
        yield resp.content

    @property
    def cumulative_usage(self) -> TokenUsage:
        """该 LLM 实例累计的 Token 用量"""
        return self._cumulative_usage

    async def _record_usage(self, usage: TokenUsage) -> None:
        """子类在 chat() 完成后调用，累加用量并触发回调。"""
        self._cumulative_usage = self._cumulative_usage + usage
        if self.on_tokens is not None:
            result = self.on_tokens(usage, self.provider_name, self.model)
            if hasattr(result, "__await__"):
                await result
