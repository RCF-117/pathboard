"""Claude adapter (Anthropic Messages API, forced tool use).

The API key is read by the SDK from the ``ANTHROPIC_API_KEY`` environment
variable.  It is never written to configs, logs or manifests.

Current Claude models do not take sampling parameters such as temperature, and
recent SDK versions no longer accept them as arguments.  Anything extra that a
particular model needs can be passed through ``extra_body``; it is recorded in
the run manifest with the rest of the config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .base import LLMReply


@dataclass(frozen=True)
class AnthropicConfig:
    model: str  # exact model id; no default on purpose
    max_tokens: int
    max_retries: int  # SDK-level retries for transient API errors
    timeout_s: float
    extra_body: dict[str, Any]  # merged into the request as-is; {} for none


class AnthropicClient:
    def __init__(self, cfg: AnthropicConfig):
        if "ANTHROPIC_API_KEY" not in os.environ:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set; export it in your shell before running"
            )
        import anthropic  # imported here so everything else works without the SDK installed

        self.cfg = cfg
        self.name = f"anthropic:{cfg.model}"
        self._client = anthropic.Anthropic(max_retries=cfg.max_retries, timeout=cfg.timeout_s)

    def decide(self, system: str, user: str, tool: dict[str, Any]) -> LLMReply:
        msg = self._client.messages.create(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            extra_body=self.cfg.extra_body or None,
        )
        calls = [b for b in msg.content if b.type == "tool_use"]
        if len(calls) != 1:
            raise RuntimeError(
                f"expected exactly one tool call, got {len(calls)} (stop_reason={msg.stop_reason})"
            )
        u = msg.usage
        input_tokens = (
            u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0)
        )
        return LLMReply(
            tool_input=dict(calls[0].input),
            input_tokens=input_tokens,
            output_tokens=u.output_tokens,
        )
