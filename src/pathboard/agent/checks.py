"""The check before each action: should it be stopped because something changed?

Modes (the agent loop's ``mitigation``):

  none                  never stop.
  recheck_any           ground truth: anything changed since this step's look.
  recheck_target        ground truth: the target changed since this step's look (or the handle
                        now points at another element).
  recheck_observed      the Experiment 2 observer in the agent's own tab (page model plus
                        network replies): an observed data change since this step's look touches
                        the target.  Knows only what reached the page.
  recheck_move_target   ground truth: another worker changed the target since the look at which
                        the current move began (or this step's look, outside a move).
  recheck_move_observed the network observer in the agent's own tab, over the same window as
                        ``recheck_move_target``.  Network only: the page model would see the
                        agent's own editor replace the cell it is editing and stop every move.

  recheck_version       read marks with versions, as the design doc specifies: the target's
                        value as the agent's page held it at this step's look (the page's own
                        data, rebuilt from its API replies) is compared with the latest value.
                        The latest value is read from the server, standing in for the board's
                        registry, which writers update right after each action.  Unlike
                        ``recheck_target`` it also catches a change made before the look that
                        had not reached the page yet.
  recheck_move_version  the same, from the look at which the current move began: the
                        validate-at-save check of optimistic concurrency control.

  recheck_move_registry like ``recheck_move_version``, but the latest value comes from the
                        board's registry (``live.registry``), which writers keep by publishing,
                        right after each action of theirs, what their own page now shows for the
                        paths it edited.  Needs no access to the app's server; knows only what
                        the board's own workers wrote.
  recheck_move_registry_page
                        the registry, plus the agent's own page: also stop if the page now shows
                        a different value for the target than at the look where the move began.
                        Catches a change made outside the board once it reaches the page.

The ``move`` modes are the "validate at save" alternative to holding a move: a worker
keeps no lock across its move, but its write is refused if what it read when the move began
has changed since.  Combined with a per-action lock, the check and the write cannot be
separated by another worker's write.

A move begins at an action that opens an edit (the board's own markers, ``live.board``) and
ends at a write of stored data, a navigation, or the edit closing (another write to the view
path that opened it).  Its window starts at the look *before* that action, because that is when
the agent read the value it is about to change.  A stop does not end the move: what the agent
typed is still in the open editor, built on the old read, so pressing save again must be
refused again until the agent closes the edit and starts over from a new look.

Every ``stop`` also returns a record of what each detector saw, for diagnosis.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from ..browser.groundtruth import GroundTruthProbe, Mark
from ..browser.observer import NetworkModel, Observer, _diff
from ..browser.session import Tab
from ..live.registry import Registry, under
from ..paths import any_conflict
from ..tasks.model import is_derived

Mitigation = Literal[
    "none",
    "recheck_any",
    "recheck_target",
    "recheck_observed",
    "recheck_move_target",
    "recheck_move_observed",
    "recheck_version",
    "recheck_move_version",
    "recheck_move_registry",
    "recheck_move_registry_page",
]
MITIGATIONS: tuple[str, ...] = (
    "none",
    "recheck_any",
    "recheck_target",
    "recheck_observed",
    "recheck_move_target",
    "recheck_move_observed",
    "recheck_version",
    "recheck_move_version",
    "recheck_move_registry",
    "recheck_move_registry_page",
)
REGISTRY_MODES = ("recheck_move_registry", "recheck_move_registry_page")

_EDIT_START = re.compile(
    r"^view/[^/]+/(editing|modal|form/.*|newline/.*|newinvoice/.*|payments/selection)$"
)
_ROUTE = re.compile(r"^view/[^/]+/route$")


class ActionCheck:
    def __init__(
        self, mode: Mitigation, tab: Tab, probe: GroundTruthProbe, registry: Registry | None
    ):
        if mode not in MITIGATIONS:
            raise ValueError(f"unknown mitigation {mode!r}")
        if (mode in REGISTRY_MODES) != (registry is not None):
            raise ValueError(
                f"mitigation {mode!r} needs a registry exactly when it is a registry mode"
            )
        self.mode, self.tab, self.probe, self.registry = mode, tab, probe, registry
        self.pre_act: dict[str, Any] | None = None  # registry modes: the page just before acting
        self.act_targets: tuple[str, ...] = ()  # registry modes: what the action acted on
        # recheck_observed: the Experiment 2 observer, opened at each look, closed at the check.
        self.observer = (
            Observer(tab, ("model_diff", "network")) if mode == "recheck_observed" else None
        )
        self.observer_open = False
        # recheck_move_observed: one running copy of the app's data rebuilt from the page's own
        # API replies, snapshotted at each look; no gaps between windows.
        self.net = (
            NetworkModel()
            if mode
            in ("recheck_move_observed", "recheck_version", "recheck_move_version", *REGISTRY_MODES)
            else None
        )
        self.look_snap: dict[str, Any] | None = None
        self.move_snap: dict[str, Any] | None = None
        self.seen: Mark | None = None  # this step's look
        self.in_move = False
        self.move_seen: Mark | None = None  # the look at which the current move began
        self.move_paths: frozenset[str] = frozenset()  # the view paths that opened it
        self.move_targets: frozenset[str] = frozenset()  # what the action that opened it targeted
        self.last_targets: tuple[str, ...] = ()
        self.moving = mode in (
            "recheck_move_target",
            "recheck_move_observed",
            "recheck_move_version",
            *REGISTRY_MODES,
        )

    # ---- called by the agent loop ---------------------------------------------------------
    def look(self, seen: Mark) -> None:
        """After each observation."""
        self.seen = seen
        if self.net is not None:
            self.look_snap = self._net_flat()
        if self.observer is not None:
            if self.observer_open:
                self.observer.end()
            self.observer.begin()
            self.observer_open = True

    def stop(
        self,
        has_target: bool,
        intended: str | None,
        actual: str | None,
        target_paths: tuple[str, ...],
        *,
        closes_edit: bool,
    ) -> tuple[bool, dict[str, Any]]:
        """Just before the action: stop it?  Returns the decision and what was seen.

        An action that closes an edit (Escape) writes nothing and is how the agent abandons a
        stale edit, so it is never stopped.  In a move (``move`` modes) the check also covers
        what the action that opened the edit targeted: the save may land on an element the page
        has redrawn, whose target cannot be resolved, but it still saves that edit.
        """
        actor = self.tab.actor
        self.last_targets = target_paths
        if self.moving and self.in_move:
            target_paths = tuple(sorted(set(target_paths) | self.move_targets))
            has_target = has_target or bool(self.move_targets)
        targeted = has_target and bool(target_paths)
        ref = self.move_seen if (self.moving and self.in_move) else self.seen
        gt_look = self.probe.foreign_data_writes_since(self.seen, actor)
        gt_move = self.probe.foreign_data_writes_since(ref, actor)
        observed: frozenset[str] = frozenset()
        if self.observer is not None and self.observer_open:
            observed = self.observer.end()
            self.observer_open = False
        snap = self.move_snap if (self.moving and self.in_move) else self.look_snap
        if self.mode == "recheck_move_observed":
            observed = frozenset(_diff(snap, self._net_flat()))
        stale_version = (
            self.mode in ("recheck_version", "recheck_move_version")
            and targeted
            and any(
                snap.get(k) != v for k, v in under(self.probe.server.flat(), target_paths).items()
            )
        )
        registry_stale, page_changed = self._registry_check(snap, target_paths, targeted)
        seen_data = {p for p in observed if p.startswith("data/")}
        if self.mode == "none" or closes_edit:
            decision = False
        elif self.mode in ("recheck_any", "recheck_target"):
            changed_view = self.probe.view_writes_since(self.seen)
            if self.mode == "recheck_any":
                decision = bool(gt_look or changed_view)
            else:
                decision = has_target and (
                    actual != intended
                    or (bool(target_paths) and any_conflict(gt_look | changed_view, target_paths))
                )
        elif self.mode == "recheck_observed":
            decision = targeted and any_conflict(seen_data, target_paths)
        elif self.mode == "recheck_move_target":
            decision = targeted and any_conflict(gt_move, target_paths)
        elif self.mode == "recheck_move_observed":
            decision = targeted and any_conflict(seen_data, target_paths)
        elif self.mode == "recheck_move_registry":
            decision = registry_stale
        elif self.mode == "recheck_move_registry_page":
            decision = registry_stale or page_changed
        else:  # recheck_version, recheck_move_version
            decision = stale_version
        log = {
            "mode": self.mode,
            "in_move": self.in_move,
            "target": list(target_paths),
            "stopped": decision,
            "closes_edit": closes_edit,
            "observed_data": sorted(seen_data),
            "gt_foreign_since_look": sorted(gt_look),
            "gt_foreign_since_move": sorted(gt_move),
            "target_changed_since_look": targeted and any_conflict(gt_look, target_paths),
            "target_changed_since_move": targeted and any_conflict(gt_move, target_paths),
            "page_version_stale": stale_version,
            "registry_stale": registry_stale,
            "page_changed": page_changed,
        }
        return decision, log

    def _registry_check(
        self, snap: dict[str, Any] | None, target_paths: tuple[str, ...], targeted: bool
    ) -> tuple[bool, bool]:
        """Registry modes: (the registry has a newer value, the agent's page now differs)."""
        if self.mode not in REGISTRY_MODES:
            return False, False
        page_now = self._net_flat()
        self.pre_act, self.act_targets = page_now, target_paths
        if not targeted:
            return False, False
        newer = any(snap.get(k) != v for k, v in self.registry.latest(target_paths).items())
        return newer, under(snap, target_paths) != under(page_now, target_paths)

    def acted(self, view_writes: set[str], data_writes: set[str]) -> None:
        """After the action ran: publish what it wrote (registry modes), then follow the move."""
        if self.registry is not None and self.pre_act is not None and self.act_targets:
            # What this worker's own page now shows for the paths it acted on, where that
            # changed during its action: the app refreshes the page's data right after a save.
            after = under(self._net_flat(), self.act_targets)
            before = under(self.pre_act, self.act_targets)
            self.registry.publish({k: v for k, v in after.items() if before.get(k) != v})
        self.pre_act = None
        stored = any(not is_derived(p) for p in data_writes)
        navigated = any(_ROUTE.match(p) for p in view_writes)
        opened = frozenset(p for p in view_writes if _EDIT_START.match(p))
        closed = bool(opened & self.move_paths)  # the edit that opened the move closed
        if self.in_move and (stored or navigated or closed):
            self._end_move()
        elif not self.in_move and opened and not stored and not navigated:
            self.in_move = True
            self.move_seen = self.seen  # the move's window starts at the look before it
            self.move_snap = self.look_snap
            self.move_paths = opened
            self.move_targets = frozenset(self.last_targets)

    def finish(self) -> None:
        if self.observer is not None and self.observer_open:
            self.observer.end()
            self.observer_open = False

    def _end_move(self) -> None:
        self.in_move = False
        self.move_seen = None
        self.move_snap = None
        self.move_paths = frozenset()
        self.move_targets = frozenset()

    def _net_flat(self) -> dict[str, Any]:
        entries = self.tab.page.evaluate("s => window.__sw.netLog.slice(s)", self.net.consumed)
        self.net.consume(entries)
        return self.net.flat()
