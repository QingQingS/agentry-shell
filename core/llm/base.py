"""
LLM 抽象层基础接口。

设计原则：
  - 上层 Agent 调用 llm.chat(messages) 即可，不感知具体 Provider
  - 每个 Provider 子类实现 chat() 异步方法，返回 LLMResponse(content, usage)
  - Token 计数由各 Provider 从 API 响应中提取，归一化为 TokenUsage
  - 通过 on_tokens 回调把每次调用的 usage 转发给上层（如 Agent → yield event）
"""

from __future__ import annotations

import time
from abc import ABC
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, List, Literal, Optional

from core.trace import LLMTracer


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
    # 不透明的推理轨迹：某些思考模型（如 DeepSeek 思考态）返回 tool_call 时附带，
    # 续接对话时必须原样回传，否则 API 报错。中性类型只存不解释，仅相关 Provider 读写。
    reasoning_content: Optional[str] = None

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
    reasoning_content: Optional[str] = None                   # 思考模型的推理轨迹，需回传时用


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
        # 无损增量追踪：tap 在此基类（所有 provider 之下）→ 新 provider 实现
        # _chat_impl 即自动被记，无需各自插桩。详见 core/trace.py。
        self._tracer = LLMTracer(self.provider_name, model)

    async def chat(
        self,
        messages: List[ChatMessage],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[ToolSpec]] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """
        非流式调用（模板方法）：tap 追踪 + 委派给子类 _chat_impl。

        全系统所有 agent 都经此唯一咽喉调 LLM，故在此落「输入增量 + 完整响应」即可
        无损复盘任意一次 ReAct，且 agent 零改动。真实调用由各 Provider 的 _chat_impl
        完成，本方法不碰 wire 细节。
        """
        self._tracer.on_request(messages)
        t0 = time.monotonic()
        resp = await self._chat_impl(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            **kwargs,
        )
        self._tracer.on_response(resp, time.monotonic() - t0)
        return resp

    async def _chat_impl(
        self,
        messages: List[ChatMessage],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[ToolSpec]] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """
        单次调用的归一化实现，由各 Provider 覆盖（不设 @abstractmethod，以免
        直接覆盖 chat() 的测试 fake 因未实现本方法而无法实例化）。

        tools 非空时启用工具调用：Provider 负责把 ToolSpec 翻译成自家 wire 格式，
        并把响应里的工具调用解析回 LLMResponse.tool_calls。ReAct 循环不在此层。

        实现要点：
            1. 调用 SDK
            2. 从响应提取 usage，构造 TokenUsage
            3. await self._record_usage(usage) 触发回调与累计
            4. 返回 LLMResponse
        """
        raise NotImplementedError

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
