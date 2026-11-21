"""The monthly spending cap on paid model calls."""

from __future__ import annotations

import pytest

from pathboard.llm import budget
from pathboard.llm.base import LLMReply
from pathboard.llm.budget import BudgetedClient, BudgetExceeded, Ledger

JAN = 1767268800.0  # 2026-01-01 12:00 UTC
FEB = JAN + 40 * 86400


class _Model:
    name = "fake"

    def __init__(self):
        self.calls = 0

    def decide(self, system, user, tool):
        self.calls += 1
        return LLMReply({"action": "finish"}, input_tokens=1_000_000, output_tokens=0)


def _client(tmp_path, cap, clock=lambda: JAN, max_out=1000):
    model = _Model()
    c = BudgetedClient(model, "gpt-6.2-sol", max_out, cap, Ledger(tmp_path / "l.jsonl"), clock)
    return c, model


def test_calls_are_recorded_and_refused_once_the_cap_is_reached(tmp_path):
    c, model = _client(tmp_path, cap=4.005)  # each call costs $2.00 (1M input tokens)
    c.decide("s", "u", {})
    c.decide("s", "u", {})
    with pytest.raises(BudgetExceeded):
        c.decide("s", "u", {})  # $4 spent + up to $0.01 worst case passes $4.005
    assert model.calls == 2


def test_the_worst_case_of_the_next_call_counts(tmp_path):
    c, model = _client(tmp_path, cap=0.05, max_out=10_000)  # worst case output $0.10
    with pytest.raises(BudgetExceeded):
        c.decide("s", "u", {})
    assert model.calls == 0


def test_spend_is_shared_through_the_ledger_and_resets_each_month(tmp_path):
    month = {"t": JAN}
    a, _ = _client(tmp_path, cap=2.005, clock=lambda: month["t"])
    b, _ = _client(tmp_path, cap=2.005, clock=lambda: month["t"])
    a.decide("s", "u", {})
    with pytest.raises(BudgetExceeded):
        b.decide("s", "u", {})  # another run on the same ledger sees a's $2
    month["t"] = FEB
    b.decide("s", "u", {})


def test_unpriced_models_and_a_missing_cap_are_refused(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        BudgetedClient(_Model(), "some-new-model", 100, 5.0, Ledger(tmp_path / "l.jsonl"))
    monkeypatch.delenv("PATHBOARD_MONTHLY_BUDGET_USD", raising=False)
    with pytest.raises(BudgetExceeded):
        budget.budgeted(_Model(), "gpt-6-luna", 100)
