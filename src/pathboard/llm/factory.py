"""Build a model client from config."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ..config import from_dict
from .base import LLMClient


@dataclass(frozen=True)
class LLMConfig:
    provider: Literal["anthropic", "openai"]
    settings: dict[str, Any]  # provider-specific; validated by the provider's own config class


def make_client(cfg: LLMConfig) -> LLMClient:
    if cfg.provider == "anthropic":
        from .anthropic_client import AnthropicClient, AnthropicConfig

        return AnthropicClient(from_dict(AnthropicConfig, cfg.settings, "llm.settings"))
    if cfg.provider == "openai":
        from .openai_client import OpenAIClient, OpenAIConfig

        return OpenAIClient(from_dict(OpenAIConfig, cfg.settings, "llm.settings"))
    raise ValueError(f"unknown provider {cfg.provider!r}")
