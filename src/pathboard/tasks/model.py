"""Tasks, jobs, expectations and the checker."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from ..testbed.state import LedgerConfig, _round_half_up_div

Recover = Literal["cheap", "expensive", "final"]
ExpectationKind = Literal["equals", "early_discount"]

_DERIVED = re.compile(r"^data/(summary/.*|invoice/[^/]+/(subtotal|tax|total)|batch/[^/]+/total)$")


def is_derived(path: str) -> bool:
    """Leaves the server computes from stored fields (never set directly)."""
    return bool(_DERIVED.match(path))


@dataclass(frozen=True)
class Job:
    id: str
    goal: str  # instruction given to the worker for this job
    deps: tuple[str, ...]  # jobs that must finish first
    recover: Recover  # planner label: how costly it is to redo this job
    program: tuple[tuple[str, dict[str, Any]], ...]  # oracle solution (semantic actions)


@dataclass(frozen=True)
class Expectation:
    kind: ExpectationKind
    path: str
    value: Any  # for "equals"; None for computed kinds

    def expected(self, final: dict[str, Any]) -> Any:
        if self.kind == "equals":
            return self.value
        if self.kind == "early_discount":
            # discount = subtotal x vendor early-payment rate, half-up, from the FINAL state
            base = self.path.rsplit("/", 1)[0]
            vendor = final[f"{base}/vendor_id"]
            return _round_half_up_div(
                final[f"{base}/subtotal"] * final[f"data/vendor/{vendor}/early_discount_bp"], 10000
            )
        raise ValueError(f"unknown expectation kind {self.kind!r}")


@dataclass(frozen=True)
class Task:
    id: str
    family: str
    ledger: LedgerConfig
    goal: str  # instruction for one agent doing the whole task
    jobs: tuple[Job, ...]
    expectations: tuple[Expectation, ...]
    answer: str | None  # expected final answer, when the task asks for one
    answer_job: str | None  # the job that produces the answer

    def __post_init__(self) -> None:
        ids = [j.id for j in self.jobs]
        if len(set(ids)) != len(ids):
            raise ValueError(f"task {self.id}: duplicate job ids")
        seen: set[str] = set()
        for j in self.jobs:  # jobs must be listed in an order that respects deps
            missing = [d for d in j.deps if d not in seen]
            if missing:
                raise ValueError(
                    f"task {self.id}: job {j.id} depends on {missing} which do not precede it"
                )
            seen.add(j.id)
        paths = [e.path for e in self.expectations]
        if len(set(paths)) != len(paths):
            raise ValueError(f"task {self.id}: duplicate expectation paths")
        if (self.answer is None) != (self.answer_job is None):
            raise ValueError(f"task {self.id}: answer and answer_job must be given together")


@dataclass(frozen=True)
class CheckResult:
    success: bool
    unmet: tuple[str, ...]  # expectation paths whose final value is wrong
    collateral: tuple[str, ...]  # stored leaves changed that the task did not ask for
    answer_ok: bool | None  # None when the task has no answer


def check(
    task: Task,
    initial: dict[str, Any],
    final: dict[str, Any],
    foreign_writes: frozenset[str],
    answer: str | None,
) -> CheckResult:
    """Judge a final state.

    ``foreign_writes`` are leaves written by other participants (for example a
    scripted interferer); changes there are not counted against the agent.
    """
    expected_paths = {e.path for e in task.expectations}
    unmet = tuple(
        sorted(
            e.path
            for e in task.expectations
            if e.path not in final or final[e.path] != e.expected(final)
        )
    )
    changed = {
        k for k in set(initial) | set(final) if initial.get(k, _MISSING) != final.get(k, _MISSING)
    }
    collateral = tuple(
        sorted(
            p
            for p in changed
            if not is_derived(p) and p not in expected_paths and p not in foreign_writes
        )
    )
    answer_ok = None
    if task.answer is not None:
        # The answer must name exactly the expected invoice and no other.
        answer_ok = answer is not None and set(re.findall(r"INV-\d+", answer)) == {task.answer}
    success = not unmet and not collateral and answer_ok is not False
    return CheckResult(success, unmet, collateral, answer_ok)


_MISSING = object()
