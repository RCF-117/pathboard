"""Scripted agents: rule-based stand-ins for the model, for free safety experiments.

Lost updates are a matter of timing, not of judgement, so a scripted agent that edits the way
the model does, and waits as long as the model thinks, exposes the same races at no API cost.

``increment_policy`` solves "increase the unit price of line L by $X" the way gpt-6.2-sol was
seen to in Experiment 4: it reads the price shown in the line's cell, opens the editor, types
price + X, and presses Enter.  If its last action was not carried out (a NOTICE) or failed (the
editor vanished), it closes the editor if open, reads the price again and starts over.

``TimedScriptedClient`` waits a think time drawn from recorded model think times before each
decision, so the windows between look and act match the model's.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from ..llm.base import LLMReply

_GOAL = re.compile(r"On invoice (INV-\d+), increase the unit price of line (L\d+) by \$(\d+)\.00")


def _money(text: str) -> float:
    return float(text.replace("$", "").replace(",", ""))


def _last_history_line(head: str) -> str:
    part = head.split("ACTIONS SO FAR", 1)[1]
    part = part.split("NOTICE", 1)[0]
    lines = [ln for ln in part.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def increment_policy() -> Callable[[str], dict[str, Any]]:  # noqa: C901  one branch per page state
    state: dict[str, Any] = {"phase": "open", "value": None}

    def act(note: str, action: str, **args: Any) -> dict[str, Any]:
        return {"note": note, "action": action, **args}

    def policy(prompt: str) -> dict[str, Any]:
        head, page = prompt.split("CURRENT PAGE", 1)
        m = _GOAL.search(head)
        if m is None:
            raise ValueError("increment_policy got a goal it cannot read")
        iid, line, dollars = m.group(1), m.group(2), int(m.group(3))
        last = _last_history_line(head)
        if "\nNOTICE\n" in head or "-> stale_element" in last or "-> action_error" in last:
            state["phase"] = "open"  # the last action did not happen: read the price again
        if state["phase"] == "done":
            return act("done", "finish")

        url = re.search(r"URL: (\S+)", page)
        if url is None or url.group(1) != f"/invoices/{iid}":
            link = re.search(rf'\[(e\d+) link "{iid}"\]', page)
            if link:
                return act("open the invoice", "click", id=link.group(1))
            search = re.search(r'\[(e\d+) input "Search"', page)
            if search:
                return act("find the invoice", "fill", id=search.group(1), text=iid)
            invoices = re.search(r'\[(e\d+) link "Invoices"\]', page)
            return act("go to the list", "click", id=invoices.group(1))

        editor = re.search(rf'\[(e\d+) input "Edit unit price of line {line}"', page)
        if state["phase"] == "open":
            if editor:
                return act("close the stale editor", "press", id=editor.group(1), key="Escape")
            row = next(ln for ln in page.splitlines() if ln.startswith(f"| {line} |"))
            cells = re.findall(r'\[(e\d+) cell "([^"]*)"\]', row)
            cell_id, shown = cells[2]  # description, quantity, unit price
            state["value"] = round(_money(shown) + dollars, 2)
            state["phase"] = "type"
            return act(f"price {shown}, new {state['value']:.2f}", "click", id=cell_id)
        if not editor:  # the editor is gone (the page was redrawn): start over
            state["phase"] = "open"
            return policy(prompt)
        if state["phase"] == "type":
            state["phase"] = "save"
            return act(
                "type the new price", "fill", id=editor.group(1), text=f"{state['value']:.2f}"
            )
        state["phase"] = "done"  # phase "save"
        return act("save", "press", id=editor.group(1), key="Enter")

    return policy


POLICIES: dict[str, Callable[[], Callable[[str], dict[str, Any]]]] = {
    "increment": increment_policy,
}


def recorded_think_times(paths: Iterable[str], family: str) -> list[float]:
    """Think times of every model step on ``family`` tasks in the given traces.jsonl files."""
    out: list[float] = []
    for path in paths:
        with open(Path(path), encoding="utf-8") as fh:
            for line in fh:
                trace = json.loads(line)["trace"]
                if trace["task_id"].startswith(f"{family}-"):
                    out.extend(s["think_s"] for s in trace["steps"])
    if not out:
        raise ValueError(f"no recorded think times for family {family!r} in {list(paths)}")
    return out


class TimedScriptedClient:
    """A scripted policy that waits a recorded model think time before each decision."""

    def __init__(
        self,
        policy_name: str,
        think_times: list[float],
        rng: random.Random,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if policy_name not in POLICIES:
            raise ValueError(
                f"unknown scripted policy {policy_name!r}; choose from {list(POLICIES)}"
            )
        self.name = f"scripted:{policy_name}"
        self._policy = POLICIES[policy_name]()
        self._think = think_times
        self._rng = rng
        self._sleep = sleep

    def decide(self, system: str, user: str, tool: dict[str, Any]) -> LLMReply:
        self._sleep(self._rng.choice(self._think))
        return LLMReply(tool_input=self._policy(user), input_tokens=0, output_tokens=0)
