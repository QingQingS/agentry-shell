"""
Configuration management.

Priority: code args > environment variables > .env file > defaults
"""

import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── LLM ──────────────────────────────────────────
    llm_provider: str = field(
        default_factory=lambda: os.getenv("LLM_PROVIDER", "openai")
    )
    smart_llm_model: str = field(
        default_factory=lambda: os.getenv("SMART_LLM_MODEL", "gpt-4o")
    )
    fast_llm_model: str = field(
        default_factory=lambda: os.getenv("FAST_LLM_MODEL", "gpt-4o-mini")
    )
    llm_temperature: float = field(
        default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.7"))
    )

    # ── Server ───────────────────────────────────────
    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8000")))
    cors_origins: list = field(
        default_factory=lambda: os.getenv(
            "CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
        ).split(",")
    )

    # ── Agent ────────────────────────────────────────
    agent_class: str = field(
        default_factory=lambda: os.getenv("AGENT_CLASS", "agents.echo_agent.EchoAgent")
    )
    verbose: bool = field(
        default_factory=lambda: os.getenv("VERBOSE", "true").lower() == "true"
    )

    # ── Retriever ────────────────────────────────────
    retriever: str = field(
        default_factory=lambda: os.getenv("RETRIEVER", "arxiv,tavily")
    )
    tavily_api_key: Optional[str] = field(
        default_factory=lambda: os.getenv("TAVILY_API_KEY")
    )

    @classmethod
    def from_env(cls) -> "Config":
        return cls()

    def get_llm_api_key(self) -> Optional[str]:
        """各 Provider 的 API Key 统一从环境变量读取"""
        key_map = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "google": "GOOGLE_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
        }
        env_key = key_map.get(self.llm_provider.lower())
        return os.getenv(env_key) if env_key else None
