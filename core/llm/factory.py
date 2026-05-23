"""
LLM 工厂：根据 Config 与 tier（smart / fast）实例化对应 Provider。

用法：
    from core.llm import get_llm

    llm = get_llm(tier="smart", config=cfg, on_tokens=callback)
    resp = await llm.chat([ChatMessage(role="user", content="hi")])
    print(resp.content, resp.usage.total_tokens)

环境变量优先级：
    overrides (kwargs) > Config 实例 > 默认 .env / 环境变量
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from core.config import Config

from .anthropic_provider import AnthropicProvider
from .base import BaseLLM, TokenCallback
from .openai_provider import DeepSeekProvider, OpenAIProvider


Tier = Literal["smart", "fast"]


PROVIDER_REGISTRY: dict[str, type[BaseLLM]] = {
    "openai": OpenAIProvider,
    "deepseek": DeepSeekProvider,
    "anthropic": AnthropicProvider,
}


def register_provider(name: str, cls: type[BaseLLM]) -> None:
    """对外暴露注册接口，方便用户加自定义 Provider。"""
    PROVIDER_REGISTRY[name.lower()] = cls


def get_llm(
    tier: Tier = "smart",
    config: Optional[Config] = None,
    on_tokens: Optional[TokenCallback] = None,
    **overrides: Any,
) -> BaseLLM:
    """
    构造一个 LLM Provider 实例。

    Args:
        tier:       "smart"（用 smart_llm_model）或 "fast"（用 fast_llm_model）
        config:     Config 实例，默认从环境读
        on_tokens:  每次调用结束的 token 回调
        **overrides:
            provider:    覆盖 Config.llm_provider
            model:       覆盖 tier 对应的模型名
            api_key:     覆盖 Config.get_llm_api_key()
            temperature: 覆盖 Config.llm_temperature
            max_tokens:  显式 max_tokens
            base_url:    自定义 Base URL（如 Azure / 本地部署）
    """
    cfg = config or Config.from_env()

    provider_name = (overrides.pop("provider", None) or cfg.llm_provider).lower()
    if provider_name not in PROVIDER_REGISTRY:
        raise ValueError(
            f"未知 LLM Provider: '{provider_name}'。"
            f"已注册: {sorted(PROVIDER_REGISTRY)}"
        )
    cls = PROVIDER_REGISTRY[provider_name]

    model = overrides.pop(
        "model",
        cfg.smart_llm_model if tier == "smart" else cfg.fast_llm_model,
    )
    api_key = overrides.pop("api_key", cfg.get_llm_api_key())
    temperature = overrides.pop("temperature", cfg.llm_temperature)
    max_tokens = overrides.pop("max_tokens", None)
    base_url = overrides.pop("base_url", None)

    return cls(
        model=model,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
        base_url=base_url,
        on_tokens=on_tokens,
        **overrides,
    )
