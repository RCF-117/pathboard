"""OpenAI adapter: request shape and reply parsing, against a fake SDK client (no network)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from pathboard.agent.loop import ACT_TOOL, SYSTEM_PROMPT
from pathboard.config import ConfigError, from_dict
from pathboard.llm.factory import LLMConfig, make_client
from pathboard.llm.openai_client import OpenAIClient, OpenAIConfig

SETTINGS: dict[str, Any] = {
    "model": "test-model",
    "max_output_tokens": 4096,
    "reasoning_effort": "medium",
    "store": False,
    "max_retries": 4,
    "timeout_s": 120,
    "extra_body": {},
}


def _call(name: str, arguments: str) -> SimpleNamespace:
    return SimpleNamespace(type="function_call", name=name, arguments=arguments, call_id="c1")


def _response(output: list[Any], status: str = "completed", reason: str | None = None):
    return SimpleNamespace(
        status=status,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        error=None,
        output=output,
        usage=SimpleNamespace(input_tokens=1200, output_tokens=85),
    )


class FakeSDK:
    """Records each request and returns the queued responses in order."""

    def __init__(self, *responses: Any):
        self.requests: list[dict[str, Any]] = []
        self._responses = list(responses)
        self.responses = self

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        return self._responses.pop(0)


def _client(*responses: Any, **overrides: Any) -> tuple[OpenAIClient, FakeSDK]:
    sdk = FakeSDK(*responses)
    cfg = from_dict(OpenAIConfig, {**SETTINGS, **overrides}, "llm.settings")
    return OpenAIClient(cfg, sdk_client=sdk), sdk


def test_request_shape_matches_the_design():
    args = {"note": "open it", "action": "click", "id": "e12"}
    client, sdk = _client(_response([_call("act", json.dumps(args))]))
    client.decide(SYSTEM_PROMPT, "TASK ...", ACT_TOOL)
    req = sdk.requests[0]
    assert req["model"] == "test-model"
    # Fixed system prompt first, as a developer message (cache-friendly), then the changing page.
    assert req["input"] == [
        {"role": "developer", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "TASK ..."},
    ]
    assert "instructions" not in req
    assert req["tools"] == [
        {
            "type": "function",
            "name": "act",
            "description": ACT_TOOL["description"],
            "parameters": ACT_TOOL["input_schema"],
            "strict": False,
        }
    ]
    assert req["tool_choice"] == {"type": "function", "name": "act"}
    assert req["reasoning"] == {"effort": "medium"}
    assert req["max_output_tokens"] == 4096
    assert req["store"] is False
    assert "temperature" not in req and "extra_body" not in req


def test_reply_is_parsed_into_tool_input_and_tokens():
    args = {"note": "type it", "action": "fill", "id": "e3", "text": "INV-1042"}
    client, _ = _client(
        _response([SimpleNamespace(type="reasoning"), _call("act", json.dumps(args))])
    )
    reply = client.decide(SYSTEM_PROMPT, "page", ACT_TOOL)
    assert reply.tool_input == args
    assert (reply.input_tokens, reply.output_tokens) == (1200, 85)
    assert client.name == "openai:test-model"


def test_null_reasoning_effort_sends_no_reasoning_field_and_extra_body_passes_through():
    client, sdk = _client(
        _response([_call("act", '{"note": "n", "action": "wait"}')]),
        reasoning_effort=None,
        extra_body={"service_tier": "default"},
    )
    client.decide("s", "u", ACT_TOOL)
    assert "reasoning" not in sdk.requests[0]
    assert sdk.requests[0]["extra_body"] == {"service_tier": "default"}


@pytest.mark.parametrize(
    "response, message",
    [
        (_response([], status="incomplete", reason="max_output_tokens"), "max_output_tokens"),
        (_response([]), r"got \[\]"),
        (_response([_call("act", "{}"), _call("act", "{}")]), "exactly one"),
        (_response([_call("other", "{}")]), "exactly one"),
        (_response([_call("act", "{not json")]), "not valid JSON"),
        (_response([_call("act", "[1, 2]")]), "not a JSON object"),
    ],
)
def test_bad_replies_raise_instead_of_becoming_actions(response, message):
    client, _ = _client(response)
    with pytest.raises(RuntimeError, match=message):
        client.decide("s", "u", ACT_TOOL)


def test_missing_key_fails_before_any_request(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is not set"):
        make_client(LLMConfig(provider="openai", settings=SETTINGS))


def test_config_is_strict():
    with pytest.raises(ConfigError):
        from_dict(OpenAIConfig, {k: v for k, v in SETTINGS.items() if k != "reasoning_effort"}, "x")
    with pytest.raises(ConfigError):
        from_dict(OpenAIConfig, {**SETTINGS, "temperature": 0.0}, "x")


def test_service_side_incomplete_reply_is_retried_then_used():
    args = {"note": "n", "action": "wait"}
    client, sdk = _client(
        _response([], status="incomplete", reason="max_messages"),
        _response([_call("act", json.dumps(args))]),
    )
    assert client.decide("s", "u", ACT_TOOL).tool_input == args
    assert len(sdk.requests) == 2


def test_incomplete_reply_gives_up_after_the_retries():
    bad = [_response([], status="incomplete", reason="max_messages") for _ in range(3)]
    client, sdk = _client(*bad)
    with pytest.raises(RuntimeError, match="max_messages"):
        client.decide("s", "u", ACT_TOOL)
    assert len(sdk.requests) == 3


def test_own_output_limit_is_not_retried():
    client, sdk = _client(_response([], status="incomplete", reason="max_output_tokens"))
    with pytest.raises(RuntimeError, match="max_output_tokens"):
        client.decide("s", "u", ACT_TOOL)
    assert len(sdk.requests) == 1
