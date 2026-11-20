"""Shared episode runners used by the experiments.

One place decides how an environment is started, how a whole task or a single
job is run, and how the outcome is judged, so that every experimental
condition goes through identical code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Callable

from .agent.loop import AgentConfig, Mitigation, run_episode
from .browser.session import BrowserSession, HandleMode, Tab
from .llm.base import LLMClient
from .tasks.model import CheckResult, Task, check
from .tasks.oracle import SyntheticTiming, run_program
from .testbed.server import ServerConfig, TestbedServer
from .trace import EpisodeTrace

TAB_LABEL = "T"
START_PATH = "/invoices"
WHOLE = "__whole__"


@dataclass(frozen=True)
class LLMAgentSettings:
    history_actions: int
    max_steps_whole: int  # step limit when one agent does the whole task
    max_steps_job: int  # step limit for a single job


def validate_timing(server: ServerConfig, settle_quiet_ms: int) -> None:
    """Polling more often than the settle window would stop pages from ever settling."""
    if server.poll_interval_ms != 0 and server.poll_interval_ms < 3 * settle_quiet_ms:
        raise ValueError(
            f"poll_interval_ms={server.poll_interval_ms} must be 0 or at least three times "
            f"settle_quiet_ms={settle_quiet_ms}"
        )


def open_tab(
    session: BrowserSession, server: TestbedServer, actor: str, handle_mode: HandleMode
) -> Tab:
    tab = session.new_tab(server.base_url, TAB_LABEL, actor, handle_mode)
    tab.open(START_PATH)
    return tab


def trace_seconds(trace: EpisodeTrace) -> float:
    """Agent time of an episode: observing, thinking and acting; harness bookkeeping excluded."""
    return sum(s.observe_s + s.think_s + s.act_s for s in trace.steps)


def judge(
    task: Task, initial: dict[str, Any], server: TestbedServer, actor: str, answer: str | None
) -> CheckResult:
    foreign = frozenset(
        w for e in server.oplog_since(0) if e["actor"] != actor for w in e["writes"]
    )
    return check(task, initial, server.flat(), foreign, answer)


def run_whole_llm(
    session: BrowserSession,
    server: TestbedServer,
    task: Task,
    repetition: int,
    llm: LLMClient,
    settings: LLMAgentSettings,
    handle_mode: HandleMode,
    mitigation: Mitigation,
    hook: Callable[[int, str | None], None] | None,
    transcript: Path | None,
    on_tab: Callable[[Tab], None] | None,
    before_observe: Callable[[int], None] | None = None,
) -> EpisodeTrace:
    """One agent does the whole task in one tab."""
    tab = open_tab(session, server, "agent", handle_mode)
    if on_tab is not None:
        on_tab(tab)
    try:
        return run_episode(
            tab,
            server,
            llm,
            task.goal,
            task.id,
            WHOLE,
            repetition,
            AgentConfig(settings.max_steps_whole, settings.history_actions),
            mitigation,
            hook,
            transcript,
            before_observe,
        )
    finally:
        session.close_tab(tab)


def run_jobs_llm(
    session: BrowserSession,
    server: TestbedServer,
    task: Task,
    repetition: int,
    llm: LLMClient,
    settings: LLMAgentSettings,
    handle_mode: HandleMode,
    transcript: Path | None,
) -> list[EpisodeTrace]:
    """A fresh agent in a fresh tab does each job, one after another, in task order."""
    traces = []
    for job in task.jobs:
        tab = open_tab(session, server, "agent", handle_mode)
        try:
            traces.append(
                run_episode(
                    tab,
                    server,
                    llm,
                    job.goal,
                    task.id,
                    job.id,
                    repetition,
                    AgentConfig(settings.max_steps_job, settings.history_actions),
                    "none",
                    None,
                    transcript,
                )
            )
        finally:
            session.close_tab(tab)
    return traces


def run_jobs_oracle(
    session: BrowserSession,
    server: TestbedServer,
    task: Task,
    repetition: int,
    timing: SyntheticTiming,
    fresh_tab_per_job: bool,
) -> list[EpisodeTrace]:
    """Run the oracle programs; one tab for everything, or a fresh tab per job."""
    traces = []
    tab = None if fresh_tab_per_job else open_tab(session, server, "agent", "ref")
    try:
        for job in task.jobs:
            job_tab = open_tab(session, server, "agent", "ref") if fresh_tab_per_job else tab
            try:
                traces.append(run_program(job_tab, server, task.id, job, repetition, timing))
            finally:
                if fresh_tab_per_job:
                    session.close_tab(job_tab)
    finally:
        if tab is not None:
            session.close_tab(tab)
    return traces


def answer_of(task: Task, traces: list[EpisodeTrace]) -> str | None:
    """The task's answer: from the whole-task episode, or from the job that owns it."""
    if task.answer is None:
        return None
    for t in traces:
        if t.job_id in (WHOLE, task.answer_job):
            return task.answer if t.agent == "oracle" else t.answer
    return None
