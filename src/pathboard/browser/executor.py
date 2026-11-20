"""Semantic actions: scripted, exact operations on the Ledger page.

Used by the scripted action catalog (Experiment 2), by oracle programs that
prove tasks are solvable, and by the scripted interferer (Experiment 3).  They
locate elements through the ground-truth registry, so they always hit the
intended element; agents never use this module.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any
from collections.abc import Callable

from .session import Tab


class SemanticExecutor:
    def __init__(self, tab: Tab):
        self.tab = tab
        self._ops: dict[str, Callable[..., None]] = {
            "goto": self.goto,
            "reset_view": self.reset_view,
            "search": self.search,
            "filter_status": self.filter_status,
            "sort": self.sort,
            "next_page": self.next_page,
            "prev_page": self.prev_page,
            "open_invoice": self.open_invoice,
            "back": self.back,
            "inline_edit": self.inline_edit,
            "begin_edit": self.begin_edit,
            "cancel_edit": self.cancel_edit,
            "select_rows": self.select_rows,
            "bulk_set": self.bulk_set,
            "detail_set": self.detail_set,
            "detail_save": self.detail_save,
            "detail_discard": self.detail_discard,
            "line_edit": self.line_edit,
            "line_add": self.line_add,
            "line_remove": self.line_remove,
            "vendor_edit": self.vendor_edit,
            "new_invoice": self.new_invoice,
            "delete_invoice": self.delete_invoice,
            "pay": self.pay,
        }

    @property
    def op_names(self) -> tuple[str, ...]:
        return tuple(self._ops)

    def run(self, op: str, **kwargs: Any) -> None:
        if op not in self._ops:
            raise KeyError(f"unknown semantic action {op!r}")
        self._ops[op](**kwargs)
        self.tab.settle()

    # ---- helpers ---------------------------------------------------------
    def _click(self, key: str) -> None:
        self.tab.gt_find(key).click()
        self.tab.settle()

    def _edit_cell(self, key: str, value: str) -> None:
        self._click(key)
        editor = self.tab.gt_find("editor")
        if editor.evaluate("el => el.tagName") == "SELECT":
            editor.select_option(label=value)
        else:
            editor.fill(value)
            editor.press("Enter")

    # ---- navigation and view ------------------------------------------------
    def goto(self, page: str) -> None:
        self._click(f"nav:{page}")

    def reset_view(self) -> None:
        """Invoice list, first page, no search, no status filter, sorted by id."""
        self._click("nav:invoices")
        self.tab.gt_find("ctl:search").fill("")
        self.tab.settle()
        self.tab.gt_find("ctl:status-filter").select_option(value="")
        self.tab.settle()
        view = self.tab.gt_view_leaves()[f"view/{self.tab.label}/invoices/sort"]
        if view != "id:asc":
            self._click("hdr:id")
            if self.tab.gt_view_leaves()[f"view/{self.tab.label}/invoices/sort"] != "id:asc":
                self._click("hdr:id")

    def search(self, text: str) -> None:
        self.tab.gt_find("ctl:search").fill(text)

    def filter_status(self, status: str) -> None:
        self.tab.gt_find("ctl:status-filter").select_option(value=status)

    def sort(self, column: str) -> None:
        self._click(f"hdr:{column}")

    def next_page(self) -> None:
        self._click("ctl:next")

    def prev_page(self) -> None:
        self._click("ctl:prev")

    def open_invoice(self, invoice: str) -> None:
        self._click(f"link:{invoice}")

    def back(self) -> None:
        self.tab.page.evaluate("history.back()")

    # ---- invoice list edits ---------------------------------------------------
    def inline_edit(self, invoice: str, field: str, value: str) -> None:
        self._edit_cell(f"cell:invoice:{invoice}:{field}", value)

    def begin_edit(self, invoice: str, field: str) -> None:
        self._click(f"cell:invoice:{invoice}:{field}")

    def cancel_edit(self) -> None:
        self.tab.gt_find("editor").press("Escape")

    def select_rows(self, invoices: list[str]) -> None:
        for iid in invoices:
            self._click(f"row-check:{iid}")

    def bulk_set(self, field: str, value: str) -> None:
        self.tab.gt_find("bulk:field").select_option(value=field)
        self.tab.settle()
        self.tab.gt_find("bulk:value").select_option(value=value)
        self.tab.settle()
        self._click("bulk:apply")

    def new_invoice(self, vendor_id: str, date: str, category: str, assignee: str) -> None:
        self._click("ctl:new")
        self.tab.gt_find("new:vendor_id").select_option(value=vendor_id)
        self.tab.gt_find("new:date").fill(date)
        self.tab.gt_find("new:category").select_option(value=category)
        self.tab.gt_find("new:assignee").select_option(value=assignee)
        self._click("new:create")

    def delete_invoice(self, invoice: str) -> None:
        self._click(f"del:{invoice}")
        self._click("modal:confirm")

    # ---- invoice detail ---------------------------------------------------------
    def detail_set(self, field: str, value: str) -> None:
        el = self.tab.gt_find(f"form:{field}")
        if el.evaluate("el => el.tagName") == "SELECT":
            el.select_option(value=value)
        else:
            el.fill(value)

    def detail_save(self) -> None:
        self._click("form:save")

    def detail_discard(self) -> None:
        self._click("form:cancel")

    def line_edit(self, line: str, field: str, value: str) -> None:
        self._edit_cell(f"line:{line}:{field}", value)

    def line_add(self, description: str, qty: str, unit_price: str) -> None:
        self.tab.gt_find("newline:description").fill(description)
        self.tab.gt_find("newline:qty").fill(qty)
        self.tab.gt_find("newline:unit_price").fill(unit_price)
        self._click("newline:add")

    def line_remove(self, line: str) -> None:
        self._click(f"line-del:{line}")

    # ---- vendors and payments -----------------------------------------------------
    def vendor_edit(self, vendor: str, field: str, value: str) -> None:
        self._edit_cell(f"cell:vendor:{vendor}:{field}", value)

    def pay(self, invoices: list[str]) -> None:
        for iid in invoices:
            self._click(f"pay-check:{iid}")
        self._click("pay:create")
        self._click("modal:confirm")


class ApiActor:
    """Another participant that changes the ledger through the application API."""

    def __init__(self, base_url: str, actor: str):
        self.base_url = base_url
        self.actor = actor

    def call(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        """Make one API call and return the JSON reply; HTTP errors raise."""
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "X-Actor": self.actor},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
