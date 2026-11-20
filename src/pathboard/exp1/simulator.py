"""Trace-driven discrete-event simulation of several workers on one workspace.

Input: for every job of a task, the recorded steps of one agent doing that job
alone (how long it observed, thought and acted, what the page showed, what the
step wrote).  The simulator replays those steps with W workers under a
coordination policy and reports the makespan.

Model
-----
* A job becomes ready when all its dependencies have finished.  Ready jobs are
  given to idle workers in task order.  Starting a job costs
  ``job_start_overhead_s`` (opening a tab or copy of the view).
* Every worker has its own view (tab).  Workers share the data.  So the only
  interaction between workers is through ``data/...`` paths.
* A step is: observe (o), think (t), then ask to act; the action takes (a) and
  its writes become visible to everyone when it ends.
* A policy is a lock rule plus a staleness rule.

  Lock rule
    global  one action at a time in the whole workspace (FIFO).
    path    an action waits only while another worker's running action holds
            a stored leaf this step also writes, or is changing something this
            step reads.  Derived values (totals, summaries) are never held:
            two edits that both move a summary do not block each other.
  Staleness rule: must the worker look again before acting, because of writes
  that landed after its observation?
    counter any data write by another worker
    view    a write that conflicts with anything its page showed
    target  a write that conflicts with the element it is about to use or
            with what the step itself writes
* Looking again costs ``o + recheck_think_fraction * t``; then the worker asks
  again.

Two reference policies need no rules: ``serial`` is one worker; ``ideal``
ignores all interaction (only job dependencies and the number of workers
limit it), which is a lower bound for list scheduling of these jobs.

What this does and does not show
--------------------------------
The replay assumes the agent would take the same steps after looking again,
and that a step's writes are known when it asks to act (a perfect declared
effect).  Both favour the ``path`` rule, so its results are an upper bound on
what a real board could save.  Workers sharing a single view cannot be
replayed from single-agent traces and are not simulated.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass
from typing import Literal

from ..paths import PathIndex

LockRule = Literal["none", "global", "path"]
StaleRule = Literal["none", "counter", "view", "target"]


@dataclass(frozen=True)
class Policy:
    name: str
    lock: LockRule
    stale: StaleRule

    def __post_init__(self) -> None:
        if self.lock not in ("none", "global", "path") or self.stale not in ("none", "counter", "view", "target"):
            raise ValueError(f"bad policy {self}")


IDEAL = Policy("ideal", "none", "none")


@dataclass(frozen=True)
class SimStep:
    observe_s: float
    think_s: float
    act_s: float
    reads_view: frozenset[str]  # data leaves shown on the page when observed
    reads_target: frozenset[str]  # data paths of the element acted on
    writes: frozenset[str]  # every data leaf the step changed, stored and derived
    holds: frozenset[str]  # the stored leaves among ``writes``: what the step must hold

    def __post_init__(self) -> None:
        for p in itertools.chain(self.reads_view, self.reads_target, self.writes):
            if not p.startswith("data/"):
                raise ValueError(f"simulation steps carry data paths only, got {p!r}")
        if not self.holds <= self.writes:
            raise ValueError("holds must be a subset of writes")


@dataclass(frozen=True)
class SimJob:
    id: str
    deps: tuple[str, ...]
    steps: tuple[SimStep, ...]

    @property
    def duration(self) -> float:
        return sum(s.observe_s + s.think_s + s.act_s for s in self.steps)


@dataclass(frozen=True)
class SimResult:
    makespan_s: float
    rechecks: int
    recheck_s: float
    wait_s: float  # time spent queued for a lock or hold
    actions: int


# Event kinds, in the order they are processed at equal times: an action that
# ends at time T is visible to a request made at time T.
_END_ACT, _REQUEST, _BEGIN_STEP = 0, 1, 2


def simulate(jobs: tuple[SimJob, ...], n_workers: int, policy: Policy, recheck_think_fraction: float,
             job_start_overhead_s: float) -> SimResult:
    if n_workers < 1:
        raise ValueError("n_workers must be >= 1")
    if not 0.0 <= recheck_think_fraction <= 1.0:
        raise ValueError("recheck_think_fraction must be in [0, 1]")
    if job_start_overhead_s < 0.0:
        raise ValueError("job_start_overhead_s must be >= 0")
    by_id = {j.id: j for j in jobs}
    if len(by_id) != len(jobs):
        raise ValueError("duplicate job ids")
    for j in jobs:
        if not j.steps:
            raise ValueError(f"job {j.id} has no steps")
        for d in j.deps:
            if d not in by_id:
                raise ValueError(f"job {j.id} depends on unknown job {d}")

    events: list[tuple[float, int, int, int]] = []  # (time, kind, tiebreak, worker)
    tiebreak = itertools.count()
    push = lambda t, kind, w: heapq.heappush(events, (t, kind, next(tiebreak), w))  # noqa: E731

    pending = [j.id for j in jobs]
    done: set[str] = set()
    idle = list(range(n_workers))
    job_of: dict[int, SimJob] = {}
    step_of: dict[int, int] = {}
    seen_at: dict[int, float] = {}  # when the worker last observed the page
    queued_since: dict[int, float] = {}  # workers waiting for a lock/hold, in arrival order
    acting: dict[int, SimStep] = {}  # workers whose action is running
    commits: list[tuple[float, int, frozenset[str]]] = []  # (time, worker, data writes)
    stats = {"rechecks": 0, "recheck_s": 0.0, "wait_s": 0.0, "actions": 0}
    finish_time = 0.0

    def dispatch(now: float) -> None:
        for jid in list(pending):
            if not idle:
                return
            if all(d in done for d in by_id[jid].deps):
                pending.remove(jid)
                w = idle.pop(0)
                job_of[w], step_of[w] = by_id[jid], 0
                push(now + job_start_overhead_s, _BEGIN_STEP, w)

    def step(w: int) -> SimStep:
        return job_of[w].steps[step_of[w]]

    def read_set(s: SimStep) -> frozenset[str]:
        if policy.stale == "view":
            return s.reads_view | s.holds
        if policy.stale == "target":
            return s.reads_target | s.holds
        return frozenset()

    def is_stale(w: int, now: float) -> bool:
        if policy.stale == "none":
            return False
        mine = None
        for t, other, writes in reversed(commits):
            if t <= seen_at[w]:
                break
            if other == w or not writes:
                continue
            if policy.stale == "counter":
                return True
            if mine is None:
                mine = PathIndex(read_set(step(w)))
            if any(mine.conflicts_with(p) for p in writes):
                return True
        return False

    def blocked(w: int) -> bool:
        if policy.lock == "none":
            return False
        if policy.lock == "global":
            return bool(acting)
        s = step(w)
        my_holds, my_reads = PathIndex(s.holds), PathIndex(read_set(s))
        for other in acting.values():
            # Two writers of the same stored leaf, or a writer changing what I read.
            if any(my_holds.conflicts_with(p) for p in other.holds) or any(my_reads.conflicts_with(p) for p in other.writes):
                return True
        # No new hold under a path that an earlier arrival is still waiting for.
        for v in queued_since:
            if v == w:
                break
            if any(my_holds.conflicts_with(p) for p in step(v).holds):
                return True
        return False

    def request(w: int, now: float) -> None:
        """The worker wants to act now.  Returns after scheduling what happens next."""
        s = step(w)
        if is_stale(w, now):
            cost = s.observe_s + recheck_think_fraction * s.think_s
            stats["rechecks"] += 1
            stats["recheck_s"] += cost
            if w in queued_since:
                stats["wait_s"] += now - queued_since.pop(w)
            seen_at[w] = now + s.observe_s
            push(now + cost, _REQUEST, w)
            return
        earlier_waiter = any(v != w for v in queued_since) and w not in queued_since
        if blocked(w) or (policy.lock == "global" and earlier_waiter):
            queued_since.setdefault(w, now)
            return
        if w in queued_since:
            stats["wait_s"] += now - queued_since.pop(w)
        acting[w] = s
        stats["actions"] += 1
        push(now + s.act_s, _END_ACT, w)

    dispatch(0.0)
    while events:
        now, kind, _, w = heapq.heappop(events)
        if kind == _BEGIN_STEP:
            s = step(w)
            seen_at[w] = now + s.observe_s
            push(now + s.observe_s + s.think_s, _REQUEST, w)
        elif kind == _REQUEST:
            request(w, now)
        else:  # _END_ACT
            s = acting.pop(w)
            if s.writes:
                commits.append((now, w, s.writes))
            step_of[w] += 1
            if step_of[w] == len(job_of[w].steps):
                done.add(job_of[w].id)
                finish_time = max(finish_time, now)
                del job_of[w], step_of[w], seen_at[w]
                idle.append(w)
                idle.sort()
                dispatch(now)
            else:
                push(now, _BEGIN_STEP, w)
            for v in list(queued_since):  # arrival order
                if v in queued_since:
                    request(v, now)

    if pending or job_of or queued_since:
        raise RuntimeError(f"simulation ended with unfinished work: pending={pending}, running={list(job_of)}, "
                           f"queued={list(queued_since)} (dependency cycle?)")
    return SimResult(makespan_s=finish_time, rechecks=stats["rechecks"], recheck_s=stats["recheck_s"],
                     wait_s=stats["wait_s"], actions=stats["actions"])


def lower_bound(jobs: tuple[SimJob, ...], n_workers: int, job_start_overhead_s: float) -> float:
    """max(longest dependency chain, total work / workers): no schedule can beat it."""
    by_id = {j.id: j for j in jobs}
    memo: dict[str, float] = {}

    def chain(jid: str) -> float:
        if jid not in memo:
            j = by_id[jid]
            memo[jid] = j.duration + job_start_overhead_s + max((chain(d) for d in j.deps), default=0.0)
        return memo[jid]

    total = sum(j.duration + job_start_overhead_s for j in jobs)
    return max(max(chain(j.id) for j in jobs), total / n_workers)
