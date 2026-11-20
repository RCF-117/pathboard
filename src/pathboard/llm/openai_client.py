"""OpenAI adapter (Responses API, one forced function call).

The API key is read by the SDK from the ``OPENAI_API_KEY`` environment
variable.  It is never written to configs, logs or manifests.

Request shape, matched to the Claude adapter so the two are comparable:

* The system prompt goes first in ``input`` as a ``developer`` message, not in
  the top-level ``instructions`` field.  Current models append ``instructions``
  after the input, which would put the fixed text after the changing page and
  defeat prompt caching.
* The tool is a function tool with the same JSON schema, and ``tool_choice``
  forces exactly that function, as the Claude adapter forces its tool.
  ``strict`` is off: strict mode requires every property to be required, and the
  schema keeps optional arguments (``id``, ``text`` ...) as in the Claude run.
* Current models take no sampling parameters (temperature, top_p).  Thinking is
  set with ``reasoning_effort``; it changes think time, which Experiment 1
  measures, so it is required in the config and recorded in the manifest.
* ``store`` is a config value; each request is self-contained (the agent loop
  re-sends the history as text), so nothing needs to be stored server-side.

Token counts: ``input_tokens`` includes cached input; ``output_tokens``
includes reasoning tokens, so ``max_output_tokens`` must leave room for them.
A reply cut off by that limit raises instead of being recorded as an action.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from .base import LLMReply


@dataclass(frozen=True)
class OpenAIConfig:
    model: str  # exact model id; no default on purpose
    max_output_tokens: int  # includes reasoning tokens
    reasoning_effort: str | None  # e.g. "low", "medium", "high"; null = send none (model default)
    store: bool  # let OpenAI keep the response server-side; false is enough here
    max_retries: int  # SDK-level retries for transient API errors
    timeout_s: float
    extra_body: dict[str, Any]  # merged into the request as-is; {} for none


def function_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Convert the shared tool definition (Anthropic-style) to a Responses function tool."""
    return {
        "type": "function",
        "name": tool["name"],
        "description": tool["description"],
        "parameters": tool["input_schema"],
        "strict": False,
    }


class OpenAIClient:
    def __init__(self, cfg: OpenAIConfig, sdk_client: Any = None):
        """``sdk_client`` replaces the real SDK client; tests only."""
        self.cfg = cfg
        self.name = f"openai:{cfg.model}"
        if sdk_client is not None:
            self._client = sdk_client
            return
        if "OPENAI_API_KEY" not in os.environ:
            raise RuntimeError("OPENAI_API_KEY is not set; export it in your shell before running")
        import openai  # imported here so everything else works without the SDK installed

        self._client = openai.OpenAI(max_retries=cfg.max_retries, timeout=cfg.timeout_s)

    def request(self, system: str, user: str, tool: dict[str, Any]) -> dict[str, Any]:
        """The keyword arguments of one ``responses.create`` call."""
        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "input": [
                {"role": "developer", "content": system},
                {"role": "user", "content": user},
            ],
            "tools": [function_tool(tool)],
            "tool_choice": {"type": "function", "name": tool["name"]},
            "max_output_tokens": self.cfg.max_output_tokens,
            "store": self.cfg.store,
        }
        if self.cfg.reasoning_effort is not None:
            kwargs["reasoning"] = {"effort": self.cfg.reasoning_effort}
        if self.cfg.extra_body:
            kwargs["extra_body"] = self.cfg.extra_body
        return kwargs

    def decide(self, system: str, user: str, tool: dict[str, Any]) -> LLMReply:
        resp = self._client.responses.create(**self.request(system, user, tool))
        if resp.status != "completed":
            reason = getattr(resp.incomplete_details, "reason", None) or getattr(
                resp, "error", None
            )
            raise RuntimeError(f"response not completed: status={resp.status}, reason={reason}")
        calls = [item for item in resp.output if item.type == "function_call"]
        if len(calls) != 1 or calls[0].name != tool["name"]:
            names = [c.name for c in calls]
            raise RuntimeError(f"expected exactly one call to {tool['name']!r}, got {names}")
        try:
            arguments = json.loads(calls[0].arguments)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"function call arguments are not valid JSON: {exc}") from exc
        if not isinstance(arguments, dict):
            raise RuntimeError("function call arguments are not a JSON object")
        return LLMReply(
            tool_input=arguments,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )
