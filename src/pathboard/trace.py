"""Trace schema: what one agent did, step by step, with ground-truth effects.

A trace is the raw data of Experiments 1 and 3.  Each step records how long the
agent spent observing, thinking and acting, and which resource paths the step
read and wrote according to the testbed's ground truth.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Literal

TimingSource = Literal["measured", "synthetic"]


@dataclass(frozen=True)
class StepRecord:
    index: int
    # Durations in seconds.
    observe_s: float
    think_s: float
    act_s: float
    # What the agent asked for.
    action: str  # click | fill | select | press | goto | back | wait | finish | semantic:<name>
    action_args: dict[str, Any]
    outcome: str  # ok | stale_element | action_error | rechecked | finish
    error: str | None
    # Ground truth, from the testbed (never shown to the agent or the observer).
    reads_view: tuple[str, ...]  # every leaf displayed at observation time
    reads_target: tuple[str, ...]  # ground-truth paths of the element acted on
    writes: tuple[str, ...]  # leaves this step changed (data and view)
    intended_target: tuple[str, ...]  # element's paths when it was observed
    actual_target: tuple[str, ...]  # element's paths when the action ran
    # Model usage for this step (None for scripted steps).
    input_tokens: int | None
    output_tokens: int | None

    def __post_init__(self) -> None:
        for name in ("observe_s", "think_s", "act_s"):
            v = getattr(self, name)
            if not (isinstance(v, (int, float)) and v >= 0.0):
                raise ValueError(f"step {self.index}: {name} must be >= 0, got {v!r}")

    @property
    def wrong_target(self) -> bool:
        """The action landed on a different thing than the agent looked at."""
        return (
            self.outcome == "ok"
            and len(self.intended_target) > 0
            and self.intended_target != self.actual_target
        )


@dataclass(frozen=True)
class EpisodeTrace:
    """One agent episode: a whole task, or one job of a decomposed task."""

    task_id: str
    job_id: str  # "__whole__" for a monolithic episode
    repetition: int
    agent: str  # e.g. "claude:<model id>" or "oracle"
    timing_source: TimingSource
    tab: str  # the tab label used in view paths of this episode
    steps: tuple[StepRecord, ...]
    finished: bool  # the agent called finish (did not hit the step limit)
    answer: str | None
    wall_s: float

    def to_json(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["steps"] = [dataclasses.asdict(s) for s in self.steps]
        return d

    @staticmethod
    def from_json(d: dict[str, Any]) -> "EpisodeTrace":
        steps = tuple(
            StepRecord(
                **{
                    **s,
                    "reads_view": tuple(s["reads_view"]),
                    "reads_target": tuple(s["reads_target"]),
                    "writes": tuple(s["writes"]),
                    "intended_target": tuple(s["intended_target"]),
                    "actual_target": tuple(s["actual_target"]),
                }
            )
            for s in d["steps"]
        )
        return EpisodeTrace(**{**d, "steps": steps})
