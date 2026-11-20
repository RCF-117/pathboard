"""Provider-independent interface: one forced tool call per request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class LLMReply:
    tool_input: dict[str, Any]  # arguments of the tool call the model made
    input_tokens: int  # all input tokens, cached or not
    output_tokens: int


class LLMClient(Protocol):
    name: str  # recorded in traces, e.g. "anthropic:<model id>"

    def decide(self, system: str, user: str, tool: dict[str, Any]) -> LLMReply:
        """Send one request and return the model's single tool call."""
        ...


class ScriptedClient:
    """A stand-in model driven by a Python function; for tests and dry runs only."""

    def __init__(self, name: str, policy: Callable[[str], dict[str, Any]]):
        self.name = f"scripted:{name}"
        self._policy = policy

    def decide(self, system: str, user: str, tool: dict[str, Any]) -> LLMReply:
        return LLMReply(tool_input=self._policy(user), input_tokens=0, output_tokens=0)
