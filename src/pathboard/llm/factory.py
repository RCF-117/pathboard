"""Build a model client from config."""

from __future__ import annotations

import functools
import random
from dataclasses import dataclass
from typing import Any, Literal

from ..config import from_dict
from .base import LLMClient
from .budget import budgeted


@dataclass(frozen=True)
class LLMConfig:
    provider: Literal["anthropic", "openai", "scripted"]
    settings: dict[str, Any]  # provider-specific; validated by the provider's own config class


@dataclass(frozen=True)
class ScriptedConfig:
    policy: str  # a rule-based agent from agent/scripted.py, e.g. "increment"
    think_times_from: tuple[str, ...]  # traces.jsonl files of a real model run
    think_family: str  # use the think times of steps on this task family
    seed: int


@functools.lru_cache(maxsize=8)
def _think_times(paths: tuple[str, ...], family: str) -> tuple[float, ...]:
    from ..agent.scripted import recorded_think_times

    return tuple(recorded_think_times(paths, family))


def make_client(cfg: LLMConfig, stream: str = "") -> LLMClient:
    """``stream`` names the agent (task, repetition, condition, worker) for scripted clients,
    so each one draws its own repeatable sequence of think times; real models ignore it."""
    if cfg.provider == "scripted":
        from ..agent.scripted import TimedScriptedClient

        sc = from_dict(ScriptedConfig, cfg.settings, "llm.settings")
        times = list(_think_times(sc.think_times_from, sc.think_family))
        return TimedScriptedClient(sc.policy, times, random.Random(f"{sc.seed}/{stream}"))
    if cfg.provider == "anthropic":
        from .anthropic_client import AnthropicClient, AnthropicConfig

        ac = from_dict(AnthropicConfig, cfg.settings, "llm.settings")
        return budgeted(AnthropicClient(ac), ac.model, ac.max_tokens)
    if cfg.provider == "openai":
        from .openai_client import OpenAIClient, OpenAIConfig

        oc = from_dict(OpenAIConfig, cfg.settings, "llm.settings")
        return budgeted(OpenAIClient(oc), oc.model, oc.max_output_tokens)
    raise ValueError(f"unknown provider {cfg.provider!r}")
