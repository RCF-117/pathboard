"""Ground-truth probe: what a step really read and wrote.

Harness only.  Data writes come from the server's operation log; view writes
are the net difference of the tab's view state before and after.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..testbed.server import TestbedServer
from .session import Tab


@dataclass(frozen=True)
class Mark:
    seq: int
    view: dict[str, str]


class GroundTruthProbe:
    def __init__(self, tab: Tab, server: TestbedServer):
        self.tab = tab
        self.server = server

    def reads(self) -> tuple[str, ...]:
        """Everything the page shows right now: data leaves and view leaves."""
        return tuple(sorted(set(self.tab.gt_displayed()) | set(self.tab.gt_view_leaves())))

    def mark(self) -> Mark:
        return Mark(self.server.seq(), self.tab.gt_view_leaves())

    def view_writes_since(self, mark: Mark) -> set[str]:
        now = self.tab.gt_view_leaves()
        return {k for k in set(mark.view) | set(now) if mark.view.get(k) != now.get(k)}

    def data_writes_since(self, mark: Mark, actor: str | None) -> set[str]:
        """Leaves written since ``mark`` by ``actor`` (or by anyone when None)."""
        return {
            w
            for e in self.server.oplog_since(mark.seq)
            if actor is None or e["actor"] == actor
            for w in e["writes"]
        }

    def foreign_data_writes_since(self, mark: Mark, actor: str) -> set[str]:
        return {
            w for e in self.server.oplog_since(mark.seq) if e["actor"] != actor for w in e["writes"]
        }
