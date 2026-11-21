"""A hard monthly spending cap on every paid model call.

Every real model client is wrapped in ``BudgetedClient``.  Before each call it adds up this
calendar month's spend from a ledger file shared by every run on this machine, plus the calls
still in flight in this process, plus the worst case of the new call (its prompt and its full
output allowance).  If that would pass the cap, the call is refused with ``BudgetExceeded``
and the run stops.  After each call its real cost is appended to the ledger.

The cap is the environment variable ``PATHBOARD_MONTHLY_BUDGET_USD``; paid clients refuse to
start without it.  The ledger is ``~/.pathboard/spend.jsonl`` unless
``PATHBOARD_SPEND_LEDGER`` names another file.  Prices are per token and must be listed in
``PRICES`` for the model; an unlisted model is refused rather than guessed.

Costs are list prices times the token counts the API reports, so they are an estimate of the
bill, not the bill.  Set a budget limit in the provider's own dashboard as well: it is the only
limit that also covers calls made outside this code.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .base import LLMClient, LLMReply

# USD per token (input, output), list prices.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-6-luna": (0.10e-6, 0.50e-6),
    "gpt-6.2-sol": (2.00e-6, 10.00e-6),
}
_CHARS_PER_TOKEN = 3  # conservative: more tokens than real text has, so the worst case is high


class BudgetExceeded(RuntimeError):
    pass


def _month(t: float) -> str:
    return time.strftime("%Y-%m", time.localtime(t))


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def spent(self, month: str) -> float:
        with open(self.path, encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_SH)
            return sum(r["usd"] for r in map(json.loads, fh) if r["month"] == month)

    def add(self, month: str, model: str, usd: float, tokens: tuple[int, int]) -> None:
        row = {"month": month, "model": model, "usd": usd, "in": tokens[0], "out": tokens[1]}
        with open(self.path, "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write(json.dumps(row) + "\n")


class BudgetedClient:
    def __init__(
        self,
        inner: LLMClient,
        model: str,
        max_output_tokens: int,
        cap_usd: float,
        ledger: Ledger,
        clock: Callable[[], float] = time.time,
    ):
        if model not in PRICES:
            raise ValueError(
                f"no price for model {model!r}: add it to llm/budget.py PRICES before using it"
            )
        if cap_usd <= 0:
            raise ValueError("the monthly budget must be positive")
        self.name = inner.name
        self._inner, self._model, self._max_out = inner, model, max_output_tokens
        self._cap, self._ledger, self._clock = cap_usd, ledger, clock
        self._lock = threading.Lock()
        self._in_flight = 0.0

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        p_in, p_out = PRICES[self._model]
        return input_tokens * p_in + output_tokens * p_out

    def decide(self, system: str, user: str, tool: dict[str, Any]) -> LLMReply:
        month = _month(self._clock())
        prompt_tokens = (len(system) + len(user) + len(json.dumps(tool))) // _CHARS_PER_TOKEN
        worst = self.cost(prompt_tokens, self._max_out)
        with self._lock:
            spent = self._ledger.spent(month)
            if spent + self._in_flight + worst > self._cap:
                raise BudgetExceeded(
                    f"monthly budget ${self._cap:.2f} reached: ${spent:.2f} spent in {month}, "
                    f"${self._in_flight:.2f} in flight, this call could cost up to ${worst:.3f}"
                )
            self._in_flight += worst
        try:
            reply = self._inner.decide(system, user, tool)
        finally:
            with self._lock:
                self._in_flight -= worst
        tokens = (reply.input_tokens, reply.output_tokens)
        self._ledger.add(month, self._model, self.cost(*tokens), tokens)
        return reply


def budgeted(inner: LLMClient, model: str, max_output_tokens: int) -> BudgetedClient:
    """Wrap a paid client with the cap from the environment; refuse to start without one."""
    raw = os.environ.get("PATHBOARD_MONTHLY_BUDGET_USD")
    if not raw:
        raise BudgetExceeded(
            "set PATHBOARD_MONTHLY_BUDGET_USD (e.g. 50) before running a paid model"
        )
    ledger_path = os.environ.get("PATHBOARD_SPEND_LEDGER", "~/.pathboard/spend.jsonl")
    return BudgetedClient(
        inner, model, max_output_tokens, float(raw), Ledger(Path(ledger_path).expanduser())
    )
