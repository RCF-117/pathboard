"""A scripted second participant that changes the workspace on a fixed schedule.

It never touches a leaf the task expects the agent to set, so a perfect agent
can still succeed under every kind of interference; any drop in success is the
agent failing to cope.

Kinds
-----
none             does nothing (baseline)
view_same_tab    re-sorts the invoice list in the agent's own tab, as another
                 agent sharing that view would
data_other_rows  edits the notes of an invoice the task does not involve
structural       deletes a draft invoice the task does not involve (rows shift)
same_entity      edits a different field (PO number) of an invoice the task targets
precondition     changes a value the task's answer depends on (the vendor's
                 early-payment rate); only meaningful for the early_discount family
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any, Literal

from ..browser.executor import ApiActor, SemanticExecutor
from ..browser.session import Tab
from ..tasks.model import Task
from ..testbed.server import TestbedServer

Kind = Literal[
    "none", "view_same_tab", "data_other_rows", "structural", "same_entity", "precondition"
]
When = Literal["stale_window", "before_observe"]
KINDS: tuple[str, ...] = (
    "none",
    "view_same_tab",
    "data_other_rows",
    "structural",
    "same_entity",
    "precondition",
)
ACTOR = "interferer"


@dataclass(frozen=True)
class Schedule:
    first_step: int  # agent step index of the first event
    every_steps: int  # then every this many steps
    max_events: int


class Interferer:
    def __init__(
        self, kind: Kind, schedule: Schedule, task: Task, server: TestbedServer, tab: Tab, seed: int
    ):
        if kind not in KINDS:
            raise ValueError(f"unknown interference kind {kind!r}")
        if schedule.every_steps < 1 or schedule.first_step < 0 or schedule.max_events < 0:
            raise ValueError(f"bad schedule {schedule}")
        self.kind, self.schedule, self.task, self.server, self.tab = (
            kind,
            schedule,
            task,
            server,
            tab,
        )
        self.rng = random.Random(f"{seed}/{task.id}/{kind}")
        self.api = ApiActor(server.base_url, ACTOR)
        self.targets = sorted(
            {
                m.group(1)
                for e in task.expectations
                if (m := re.match(r"data/invoice/([^/]+)/", e.path))
            }
        )
        self.events: list[dict[str, Any]] = []  # what was done, for the run log
        self._counter = 0

    def due(self, step_index: int) -> bool:
        s = self.schedule
        return (
            self.kind != "none"
            and len(self.events) < s.max_events
            and step_index >= s.first_step
            and (step_index - s.first_step) % s.every_steps == 0
        )

    def fire(self, step_index: int) -> None:
        """Make one change, then wait until the agent's page shows it."""
        flat = self.server.flat()
        invoices = sorted(
            {p.split("/")[2] for p in flat if p.startswith("data/invoice/")},
            key=lambda i: int(i[4:]),
        )
        others = [i for i in invoices if i not in self.targets]
        self._counter += 1
        done: dict[str, Any] = {"step": step_index, "kind": self.kind, "applied": True}
        if self.kind == "view_same_tab":
            if self.tab.gt_has("hdr:total"):
                column = self.rng.choice(
                    ("vendor", "date", "category", "assignee", "status", "total")
                )
                SemanticExecutor(self.tab).run("sort", column=column)
                done["detail"] = f"sorted by {column}"
            else:
                done["applied"] = False  # the agent is not on the invoice list
        elif self.kind == "data_other_rows":
            pool = [
                i
                for i in others
                if flat[f"data/invoice/{i}/status"] in ("draft", "review", "approved")
            ]
            iid = self.rng.choice(pool)
            self.api.call("PATCH", f"/api/invoices/{iid}", {"notes": f"touched {self._counter}"})
            done["detail"] = f"notes of {iid}"
        elif self.kind == "structural":
            drafts = [i for i in others if flat[f"data/invoice/{i}/status"] == "draft"]
            if drafts:
                self.api.call("DELETE", f"/api/invoices/{drafts[0]}", None)
                done["detail"] = f"deleted {drafts[0]}"
            else:
                done["applied"] = False
        elif self.kind == "same_entity":
            live = [
                i
                for i in self.targets
                if f"data/invoice/{i}/status" in flat
                and flat[f"data/invoice/{i}/status"] in ("draft", "review", "approved")
                and not any(e.path == f"data/invoice/{i}/po_number" for e in self.task.expectations)
            ]
            if live:
                iid = live[self._counter % len(live)]
                self.api.call(
                    "PATCH", f"/api/invoices/{iid}", {"po_number": f"PO-X{self._counter:04d}"}
                )
                done["detail"] = f"PO number of {iid}"
            else:
                done["applied"] = False
        elif self.kind == "precondition":
            computed = [
                e.path.split("/")[2] for e in self.task.expectations if e.kind == "early_discount"
            ]
            if computed:
                iid = computed[self._counter % len(computed)]
                vendor = flat[f"data/invoice/{iid}/vendor_id"]
                rate = flat[f"data/vendor/{vendor}/early_discount_bp"] + 25
                self.api.call("PATCH", f"/api/vendors/{vendor}", {"early_discount_bp": rate})
                done["detail"] = f"early-payment rate of {vendor} (vendor of {iid}) -> {rate} bp"
            else:
                done["applied"] = False
        self.events.append(done)
        self.tab.gt_wait_applied(self.server.seq())
