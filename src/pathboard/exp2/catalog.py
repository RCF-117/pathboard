"""Scripted action catalog for Experiment 2.

Each action type covers one kind of effect (a precise edit, a view-wide
change, navigation, an indirect change to derived values, a structural insert
or delete, a final action, a change made by someone else).  An instance fixes
concrete targets, chosen with a seeded generator from the initial ledger, so
the same instance is replayed under every rendering mode.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any
from collections.abc import Callable

from ..testbed.state import ASSIGNEES, CATEGORIES, Ledger

Op = tuple[str, dict[str, Any]]


@dataclass(frozen=True)
class ActionInstance:
    type: str
    effect: str  # the kind of effect this type stands for
    setup: tuple[Op, ...]  # semantic actions run before the observed window
    action: Op  # the observed action: a semantic action, or ("remote", {method, path, body})


def _page1(led: Ledger, page_size: int) -> list[str]:
    return sorted(led.invoices, key=lambda i: int(i[4:]))[:page_size]


def _editable(led: Ledger, ids: list[str]) -> list[str]:
    return [i for i in ids if led.invoices[i]["status"] in ("draft", "review", "approved")]


def _other(pool: tuple[str, ...], current: str, rng: random.Random) -> str:
    return rng.choice([v for v in pool if v != current])


def _need(items: list, what: str) -> list:
    if not items:
        raise ValueError(f"ledger has no {what}; use a larger ledger or another seed")
    return items


def _inline_select(led, p1, rng):
    iid = rng.choice(_need(_editable(led, p1), "editable invoice on page 1"))
    return (), (
        "inline_edit",
        {
            "invoice": iid,
            "field": "category",
            "value": _other(CATEGORIES, led.invoices[iid]["category"], rng),
        },
    )


def _inline_text(led, p1, rng):
    iid = rng.choice(_need(_editable(led, p1), "editable invoice on page 1"))
    return (), (
        "inline_edit",
        {"invoice": iid, "field": "notes", "value": f"note {rng.randint(100, 999)}"},
    )


def _inline_status(led, p1, rng):
    iid = rng.choice(
        _need(
            [i for i in p1 if led.invoices[i]["status"] in ("draft", "review")],
            "draft/review invoice",
        )
    )
    return (), ("inline_edit", {"invoice": iid, "field": "status", "value": "approved"})


def _begin_edit(led, p1, rng):
    iid = rng.choice(_need(_editable(led, p1), "editable invoice on page 1"))
    return (), (
        "begin_edit",
        {"invoice": iid, "field": rng.choice(("category", "assignee", "notes"))},
    )


def _sort(led, p1, rng):
    return (), (
        "sort",
        {"column": rng.choice(("vendor", "date", "category", "assignee", "status", "total"))},
    )


def _filter(led, p1, rng):
    return (), ("filter_status", {"status": rng.choice(("draft", "review", "approved"))})


def _search(led, p1, rng):
    return (), ("search", {"text": rng.choice(list(led.vendors.values()))["name"].split()[0]})


def _next_page(led, p1, rng):
    return (), ("next_page", {})


def _select_row(led, p1, rng):
    return (), ("select_rows", {"invoices": [rng.choice(p1)]})


def _bulk(led, p1, rng):
    ids = rng.sample(_need(_editable(led, p1), "editable invoice on page 1"), 3)
    return (("select_rows", {"invoices": ids}),), (
        "bulk_set",
        {"field": "assignee", "value": rng.choice(ASSIGNEES)},
    )


def _open(led, p1, rng):
    return (), ("open_invoice", {"invoice": rng.choice(p1)})


def _back(led, p1, rng):
    return (("open_invoice", {"invoice": rng.choice(p1)}),), ("back", {})


def _detail_draft(led, p1, rng):
    iid = rng.choice(_need(_editable(led, p1), "editable invoice on page 1"))
    return (("open_invoice", {"invoice": iid}),), (
        "detail_set",
        {"field": "notes", "value": f"draft {rng.randint(100, 999)}"},
    )


def _detail_save(led, p1, rng):
    iid = rng.choice(_need(_editable(led, p1), "editable invoice on page 1"))
    setup = (
        ("open_invoice", {"invoice": iid}),
        (
            "detail_set",
            {"field": "category", "value": _other(CATEGORIES, led.invoices[iid]["category"], rng)},
        ),
        ("detail_set", {"field": "po_number", "value": f"PO-{rng.randint(10000, 99999)}"}),
    )
    return setup, ("detail_save", {})


def _line_qty(led, p1, rng):
    iid = rng.choice(_need(_editable(led, p1), "editable invoice on page 1"))
    lid = rng.choice(list(led.invoices[iid]["lines"]))
    qty = rng.choice([q for q in range(1, 13) if q != led.invoices[iid]["lines"][lid]["qty"]])
    return (("open_invoice", {"invoice": iid}),), (
        "line_edit",
        {"line": lid, "field": "qty", "value": str(qty)},
    )


def _line_add(led, p1, rng):
    iid = rng.choice(_need(_editable(led, p1), "editable invoice on page 1"))
    return (("open_invoice", {"invoice": iid}),), (
        "line_add",
        {
            "description": "Extra item",
            "qty": str(rng.randint(1, 5)),
            "unit_price": f"{rng.randint(10, 300)}.00",
        },
    )


def _line_remove(led, p1, rng):
    iid = rng.choice(
        _need(
            [
                i
                for i in _editable(led, p1)
                if len(led.invoices[i]["lines"]) >= 2 and led.invoices[i]["discount_cents"] == 0
            ],
            "editable invoice with two lines",
        )
    )
    return (("open_invoice", {"invoice": iid}),), (
        "line_remove",
        {"line": rng.choice(list(led.invoices[iid]["lines"]))},
    )


def _vendor_tax(led, p1, rng):
    vid = rng.choice(list(led.vendors))
    new = rng.choice(
        [
            v
            for v in ("6.00", "8.25", "9.50", "11.00")
            if int(float(v) * 100) != led.vendors[vid]["tax_bp"]
        ]
    )
    return (("goto", {"page": "vendors"}),), (
        "vendor_edit",
        {"vendor": vid, "field": "tax_bp", "value": new},
    )


def _vendor_rename(led, p1, rng):
    vid = rng.choice(list(led.vendors))
    return (("goto", {"page": "vendors"}),), (
        "vendor_edit",
        {"vendor": vid, "field": "name", "value": f"Renamed {rng.randint(100, 999)}"},
    )


def _new_invoice(led, p1, rng):
    return (), (
        "new_invoice",
        {
            "vendor_id": rng.choice(list(led.vendors)),
            "date": "2026-11-05",
            "category": rng.choice(CATEGORIES),
            "assignee": rng.choice(ASSIGNEES),
        },
    )


def _delete(led, p1, rng):
    return (), (
        "delete_invoice",
        {
            "invoice": rng.choice(
                _need(
                    [i for i in p1 if led.invoices[i]["status"] == "draft"],
                    "draft invoice on page 1",
                )
            )
        },
    )


def _pay(led, p1, rng):
    approved = _need(
        [i for i in led.invoices if led.invoices[i]["status"] == "approved"], "approved invoice"
    )
    return (("goto", {"page": "payments"}),), (
        "pay",
        {"invoices": sorted(rng.sample(approved, min(2, len(approved))))},
    )


def _remote(method: str, path: str, body: dict[str, Any] | None) -> Op:
    return ("remote", {"method": method, "path": path, "body": body})


def _remote_visible(led, p1, rng):
    iid = rng.choice(_need(_editable(led, p1), "editable invoice on page 1"))
    return (), _remote(
        "PATCH",
        f"/api/invoices/{iid}",
        {"category": _other(CATEGORIES, led.invoices[iid]["category"], rng)},
    )


def _remote_hidden(led, p1, rng):
    hidden = _need(
        _editable(led, [i for i in led.invoices if i not in p1]), "editable invoice beyond page 1"
    )
    iid = rng.choice(hidden)
    return (), _remote(
        "PATCH",
        f"/api/invoices/{iid}",
        {"assignee": _other(ASSIGNEES, led.invoices[iid]["assignee"], rng)},
    )


def _remote_vendor_tax(led, p1, rng):
    vid = led.invoices[rng.choice(p1)]["vendor_id"]
    return (), _remote("PATCH", f"/api/vendors/{vid}", {"tax_bp": led.vendors[vid]["tax_bp"] + 125})


def _remote_delete(led, p1, rng):
    iid = rng.choice(
        _need([i for i in p1 if led.invoices[i]["status"] == "draft"], "draft invoice on page 1")
    )
    return (), _remote("DELETE", f"/api/invoices/{iid}", None)


def _remote_on_summary(led, p1, rng):
    iid = rng.choice(
        _need(
            [i for i in led.invoices if led.invoices[i]["status"] in ("draft", "review")],
            "draft/review invoice",
        )
    )
    return (("goto", {"page": "summary"}),), _remote(
        "PATCH", f"/api/invoices/{iid}", {"status": "void"}
    )


_Maker = Callable[[Ledger, list[str], random.Random], tuple[tuple[Op, ...], Op]]
CATALOG: dict[str, tuple[str, _Maker]] = {
    "inline_edit_select": ("precise edit", _inline_select),
    "inline_edit_text": ("precise edit", _inline_text),
    "inline_edit_status": ("precise edit", _inline_status),
    "bulk_set": ("multi-row edit", _bulk),
    "detail_save": ("form save", _detail_save),
    "line_edit_qty": ("edit with derived effects", _line_qty),
    "vendor_tax_edit": ("edit with derived effects", _vendor_tax),
    "vendor_rename": ("edit shown elsewhere", _vendor_rename),
    "line_add": ("structural insert", _line_add),
    "new_invoice": ("structural insert", _new_invoice),
    "line_remove": ("structural delete", _line_remove),
    "delete_invoice": ("structural delete", _delete),
    "pay": ("final action", _pay),
    "begin_edit": ("view only", _begin_edit),
    "select_row": ("view only", _select_row),
    "detail_draft": ("view only", _detail_draft),
    "sort": ("view-wide", _sort),
    "filter_status": ("view-wide", _filter),
    "search": ("view-wide", _search),
    "next_page": ("view-wide", _next_page),
    "open_invoice": ("navigation", _open),
    "back": ("navigation", _back),
    "remote_edit_visible": ("change by another actor", _remote_visible),
    "remote_edit_hidden": ("change by another actor", _remote_hidden),
    "remote_vendor_tax": ("change by another actor", _remote_vendor_tax),
    "remote_delete_visible": ("change by another actor", _remote_delete),
    "remote_edit_on_summary": ("change by another actor", _remote_on_summary),
}


def make_instance(
    action_type: str, led: Ledger, page_size: int, seed: int, repetition: int
) -> ActionInstance:
    if action_type not in CATALOG:
        raise KeyError(f"unknown action type {action_type!r}")
    effect, maker = CATALOG[action_type]
    rng = random.Random(f"{seed}/{action_type}/{repetition}")
    setup, action = maker(led, _page1(led, page_size), rng)
    return ActionInstance(action_type, effect, setup, action)
