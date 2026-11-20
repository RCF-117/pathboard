"""A live board: decides, before each action of each worker, whether it may run now.

Policies (the same names as the trace simulator in ``exp1/simulator.py``):

  lock   none    no coordination at all.
         global  one worker acts at a time in the whole workspace (first come, first served).
         path    a worker waits only while another worker holds a path that conflicts
                 (one is a prefix of the other) with the data paths of the element it is
                 about to act on.  Actions with no data target take no hold.
  scope  action  a lock or hold is released as soon as the action ends.
         move    when an action begins an edit (opens an editor, types into a draft) the
                 lock or holds are kept until an action of the same worker writes stored
                 data, navigates to another page, or the worker stops.  Inside its own move
                 a worker never waits; under ``path``, a hold it adds mid-move that
                 conflicts with another worker's is counted as an escalation, not waited
                 for (waiting while holding could deadlock).

The target check ("look again if what you are about to act on changed") is the agent loop's
``recheck_target`` mitigation and runs after the board has let the action through.

A lease bounds every hold: a worker that keeps a move open longer than ``lease_s`` loses it
(counted), so one stuck worker cannot stall the others forever.
"""

from __future__ import annotations

import itertools
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Literal

from ..paths import conflicts
from ..tasks.model import is_derived

Lock = Literal["none", "global", "path"]
Scope = Literal["action", "move"]

# Same markers as ``exp1.simulate.moves_of``: view leaves whose change means an edit began.
_EDIT_START = re.compile(
    r"^view/[^/]+/(editing|modal|form/.*|newline/.*|newinvoice/.*|payments/selection)$"
)
_ROUTE = re.compile(r"^view/[^/]+/route$")


@dataclass
class WorkerStats:
    wait_s: float = 0.0  # time spent blocked in before_act
    waits: int = 0  # actions that had to wait at all
    moves: int = 0  # moves that held a lock or holds past one action
    escalations: int = 0  # path holds added mid-move that conflicted with another worker's
    lease_expiries: int = 0


@dataclass
class _Worker:
    name: str
    in_move: bool = False
    holds: frozenset[str] = frozenset()
    owns_global: bool = False
    since: float = 0.0  # when the current lock or move began
    stats: WorkerStats = field(default_factory=WorkerStats)


class Board:
    def __init__(self, lock: Lock, scope: Scope, lease_s: float):
        if lock not in ("none", "global", "path") or scope not in ("action", "move"):
            raise ValueError(f"bad board policy lock={lock!r} scope={scope!r}")
        if lease_s <= 0:
            raise ValueError("lease_s must be positive")
        self.lock, self.scope, self.lease_s = lock, scope, lease_s
        self._cv = threading.Condition()
        self._workers: dict[str, _Worker] = {}
        self._queue: list[tuple[int, str, frozenset[str]]] = []  # (ticket, worker, wanted)
        self._tickets = itertools.count()

    # ---- registration --------------------------------------------------------------------
    def gate(self, worker: str) -> WorkerGate:
        with self._cv:
            if worker in self._workers:
                raise ValueError(f"worker {worker!r} already registered")
            self._workers[worker] = _Worker(worker)
        return WorkerGate(self, worker)

    def stats(self) -> dict[str, WorkerStats]:
        with self._cv:
            return {n: w.stats for n, w in self._workers.items()}

    # ---- decisions -----------------------------------------------------------------------
    def before_act(self, worker: str, target_paths: tuple[str, ...]) -> None:
        if self.lock == "none":
            return
        wanted = frozenset(p for p in target_paths if p.startswith("data/") and not is_derived(p))
        with self._cv:
            me = self._workers[worker]
            if me.in_move:  # inside its own move a worker never waits
                if self.lock == "path" and wanted - me.holds:
                    if self._conflicts_with_others(worker, wanted - me.holds):
                        me.stats.escalations += 1
                    me.holds = me.holds | wanted
                return
            ticket = next(self._tickets)
            self._queue.append((ticket, worker, wanted))
            start = time.monotonic()
            waited = False
            while not self._may_go(worker, ticket, wanted):
                waited = True
                self._expire_leases()
                self._cv.wait(timeout=0.25)
            self._queue = [q for q in self._queue if q[0] != ticket]
            if waited:
                me.stats.waits += 1
                me.stats.wait_s += time.monotonic() - start
            me.since = time.monotonic()
            if self.lock == "global":
                me.owns_global = True
            else:
                me.holds = wanted
            self._cv.notify_all()

    def after_act(self, worker: str, view_writes: set[str], data_writes: set[str]) -> None:
        if self.lock == "none":
            return
        stored = any(not is_derived(p) for p in data_writes)
        navigated = any(_ROUTE.match(p) for p in view_writes)
        began_edit = any(_EDIT_START.match(p) for p in view_writes)
        with self._cv:
            me = self._workers[worker]
            if self.scope == "move":
                if me.in_move and (stored or navigated):
                    self._release(me)
                elif not me.in_move and began_edit and not stored and not navigated:
                    me.in_move = True
                    me.stats.moves += 1
                elif not me.in_move:
                    self._release(me)
            else:
                self._release(me)
            self._cv.notify_all()

    def skipped(self, worker: str) -> None:
        """The target check stopped the action: keep a move's holds, else release."""
        if self.lock == "none":
            return
        with self._cv:
            me = self._workers[worker]
            if not me.in_move:
                self._release(me)
            self._cv.notify_all()

    def finish(self, worker: str) -> None:
        """The worker stopped (finished, gave up or failed): release everything it holds."""
        with self._cv:
            self._release(self._workers[worker])
            self._queue = [q for q in self._queue if q[1] != worker]
            self._cv.notify_all()

    # ---- internals (call with the condition held) -----------------------------------------
    def _release(self, me: _Worker) -> None:
        me.in_move, me.holds, me.owns_global = False, frozenset(), False

    def _conflicts_with_others(self, worker: str, paths: frozenset[str]) -> bool:
        return any(
            conflicts(p, q)
            for other in self._workers.values()
            if other.name != worker
            for p in paths
            for q in other.holds
        )

    def _may_go(self, worker: str, ticket: int, wanted: frozenset[str]) -> bool:
        earlier = [q for q in self._queue if q[0] < ticket]
        if self.lock == "global":
            busy = any(w.owns_global for w in self._workers.values() if w.name != worker)
            return not busy and not earlier
        if not wanted:
            return True
        if self._conflicts_with_others(worker, wanted):
            return False
        # No new hold under a path an earlier arrival is still waiting for.
        return not any(conflicts(p, q) for _, _, w in earlier for p in wanted for q in w)

    def _expire_leases(self) -> None:
        now = time.monotonic()
        for w in self._workers.values():
            if (w.owns_global or w.holds) and w.in_move and now - w.since > self.lease_s:
                w.stats.lease_expiries += 1
                self._release(w)


class WorkerGate:
    """The board as one worker sees it: what the agent loop's ``gate`` parameter expects."""

    def __init__(self, board: Board, worker: str):
        self.board, self.worker = board, worker

    def before_act(self, action: str, target_paths: tuple[str, ...]) -> None:
        self.board.before_act(self.worker, target_paths)

    def after_act(self, view_writes: set[str], data_writes: set[str]) -> None:
        self.board.after_act(self.worker, view_writes, data_writes)

    def skipped(self) -> None:
        self.board.skipped(self.worker)

    def finish(self) -> None:
        self.board.finish(self.worker)
