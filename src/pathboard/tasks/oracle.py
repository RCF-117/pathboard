"""Run a task's oracle programs through the real page.

This proves a task is solvable through the UI and that the checker accepts the
intended solution.  It also produces traces for testing the analysis pipeline;
those traces carry ``timing_source="synthetic"`` because the observe and think
durations are configured constants, not measurements of an agent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..browser.executor import SemanticExecutor
from ..browser.groundtruth import GroundTruthProbe
from ..browser.session import Tab
from ..testbed.server import TestbedServer
from ..trace import EpisodeTrace, StepRecord
from .model import Job


@dataclass(frozen=True)
class SyntheticTiming:
    observe_s: float  # stand-in for reading the page
    think_s: float  # stand-in for the model deciding


def run_program(
    tab: Tab,
    server: TestbedServer,
    task_id: str,
    job: Job,
    repetition: int,
    timing: SyntheticTiming,
) -> EpisodeTrace:
    """Execute one job's oracle program in ``tab`` and record a trace."""
    probe = GroundTruthProbe(tab, server)
    executor = SemanticExecutor(tab)
    steps = []
    started = time.monotonic()
    for index, (op, kwargs) in enumerate(job.program):
        reads = probe.reads()
        mark = probe.mark()
        t0 = time.monotonic()
        executor.run(op, **kwargs)
        act_s = time.monotonic() - t0
        tab.gt_wait_applied(server.seq())
        writes = probe.data_writes_since(mark, tab.actor) | probe.view_writes_since(mark)
        steps.append(
            StepRecord(
                index=index,
                observe_s=timing.observe_s,
                think_s=timing.think_s,
                act_s=act_s,
                action=f"semantic:{op}",
                action_args=dict(kwargs),
                outcome="ok",
                error=None,
                reads_view=reads,
                reads_target=(),
                writes=tuple(sorted(writes)),
                intended_target=(),
                actual_target=(),
                input_tokens=None,
                output_tokens=None,
            )
        )
    return EpisodeTrace(
        task_id=task_id,
        job_id=job.id,
        repetition=repetition,
        agent="oracle",
        timing_source="synthetic",
        tab=tab.label,
        steps=tuple(steps),
        finished=True,
        answer=None,
        wall_s=time.monotonic() - started,
    )
