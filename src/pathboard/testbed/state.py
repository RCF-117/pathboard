"""Ledger state: vendors, invoices with line items, and payment batches.

All money is integer cents and all rates are integer basis points, so every
derived value is exact.  ``flat()`` exposes the whole state as leaf paths; the
ground truth of what an operation changed is the diff of two ``flat()`` calls.
"""

from __future__ import annotations

import copy
import random
from dataclasses import dataclass
from typing import Any

CATEGORIES = (
    "Travel",
    "Software",
    "Hardware",
    "Consulting",
    "Marketing",
    "Facilities",
    "Legal",
    "Training",
)
ASSIGNEES = ("Avery", "Blake", "Casey", "Devon", "Emery", "Finley")
STATUSES = ("draft", "review", "approved", "paid", "void")
LOCKED_STATUSES = ("paid", "void")

_VENDOR_NAMES = (
    "Northwind Traders",
    "Acme Supply",
    "Globex Systems",
    "Initech Services",
    "Umbra Logistics",
    "Halcyon Labs",
    "Pinecrest Media",
    "Quartz Analytics",
    "Redwood Legal",
    "Summit Travel",
    "Tidewater Hardware",
    "Vertex Consulting",
    "Willow Training",
    "Zenith Facilities",
    "Bluebird Software",
    "Copperfield Print",
)
_ITEMS = (
    "License seats",
    "Support hours",
    "Onsite visit",
    "Cloud credits",
    "Laptop",
    "Monitor",
    "Airfare",
    "Hotel nights",
    "Workshop",
    "Design review",
    "Ad placement",
    "Office chairs",
    "Legal review",
    "Courier",
    "Maintenance",
    "Data export",
)

INVOICE_FIELDS = (
    "vendor_id",
    "date",
    "due_date",
    "po_number",
    "category",
    "assignee",
    "notes",
    "status",
    "discount_cents",
)
LINE_FIELDS = ("description", "qty", "unit_price_cents")
VENDOR_FIELDS = ("name", "tax_bp", "early_discount_bp", "terms_days", "contact")


class LedgerError(Exception):
    """A rejected operation. ``code`` is ``invalid``, ``not_found`` or ``locked``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LedgerConfig:
    seed: int
    n_invoices: int
    n_vendors: int
    max_lines: int
    first_invoice_number: int


def _round_half_up_div(numerator: int, denominator: int) -> int:
    """Round ``numerator / denominator`` half up, for non-negative integers."""
    if numerator < 0 or denominator <= 0:
        raise ValueError("expected non-negative numerator and positive denominator")
    return (2 * numerator + denominator) // (2 * denominator)


class Ledger:
    def __init__(self, cfg: LedgerConfig):
        if not 1 <= cfg.n_vendors <= len(_VENDOR_NAMES):
            raise ValueError(f"n_vendors must be in 1..{len(_VENDOR_NAMES)}")
        if cfg.n_invoices < 1 or cfg.max_lines < 1:
            raise ValueError("n_invoices and max_lines must be positive")
        self.cfg = cfg
        rng = random.Random(cfg.seed)
        self.vendors: dict[str, dict[str, Any]] = {}
        for i, name in enumerate(rng.sample(_VENDOR_NAMES, cfg.n_vendors), start=1):
            self.vendors[f"V-{i:02d}"] = {
                "name": name,
                "tax_bp": rng.choice((0, 500, 725, 800, 1000)),
                "early_discount_bp": rng.choice((100, 150, 200, 250, 300)),
                "terms_days": rng.choice((15, 30, 45, 60)),
                "contact": rng.choice(ASSIGNEES),
            }
        self.invoices: dict[str, dict[str, Any]] = {}
        self._next_invoice = cfg.first_invoice_number
        vendor_ids = list(self.vendors)
        for _ in range(cfg.n_invoices):
            month, day = rng.randint(1, 9), rng.randint(1, 28)
            inv = {
                "vendor_id": rng.choice(vendor_ids),
                "date": f"2026-{month:02d}-{day:02d}",
                "due_date": f"2026-{month + 1:02d}-{day:02d}",
                "po_number": f"PO-{rng.randint(10000, 99999)}",
                "category": rng.choice(CATEGORIES),
                "assignee": rng.choice(ASSIGNEES),
                "notes": "",
                "status": rng.choice(("draft", "draft", "review", "review", "approved")),
                "discount_cents": 0,
                "lines": {},
                "next_line": 1,
            }
            for _ in range(rng.randint(1, cfg.max_lines)):
                inv["lines"][f"L{inv['next_line']}"] = {
                    "description": rng.choice(_ITEMS),
                    "qty": rng.randint(1, 12),
                    "unit_price_cents": rng.randint(20, 900) * 100 + rng.choice((0, 0, 50, 99)),
                }
                inv["next_line"] += 1
            self.invoices[self._new_invoice_id()] = inv
        self.batches: dict[str, dict[str, Any]] = {}
        self._next_batch = 1

    # ---- ids -------------------------------------------------------------
    def _new_invoice_id(self) -> str:
        iid = f"INV-{self._next_invoice}"
        self._next_invoice += 1
        return iid

    # ---- derived values --------------------------------------------------
    def derived(self, iid: str) -> dict[str, int]:
        inv = self._invoice(iid)
        subtotal = sum(line["qty"] * line["unit_price_cents"] for line in inv["lines"].values())
        taxable = subtotal - inv["discount_cents"]
        tax = _round_half_up_div(taxable * self.vendors[inv["vendor_id"]]["tax_bp"], 10000)
        return {"subtotal": subtotal, "tax": tax, "total": taxable + tax}

    def invoice_record(self, iid: str) -> dict[str, Any]:
        inv = self._invoice(iid)
        rec = {k: inv[k] for k in INVOICE_FIELDS}
        rec["id"] = iid
        rec["vendor_name"] = self.vendors[inv["vendor_id"]]["name"]
        rec["lines"] = [{"id": lid, **line} for lid, line in inv["lines"].items()]
        rec.update(self.derived(iid))
        return rec

    def vendor_record(self, vid: str) -> dict[str, Any]:
        return {"id": vid, **self._vendor(vid)}

    def summary(self) -> dict[str, Any]:
        by_cat = {c: 0 for c in CATEGORIES}
        by_status = {s: 0 for s in STATUSES}
        for iid, inv in self.invoices.items():
            by_status[inv["status"]] += 1
            if inv["status"] != "void":
                by_cat[inv["category"]] += self.derived(iid)["total"]
        return {"category_total": by_cat, "status_count": by_status}

    def flat(self) -> dict[str, Any]:
        """Every leaf of the state, stored and derived, keyed by resource path."""
        out: dict[str, Any] = {"data/invoice-index": ",".join(sorted(self.invoices))}
        for iid, inv in self.invoices.items():
            base = f"data/invoice/{iid}"
            for f in INVOICE_FIELDS:
                out[f"{base}/{f}"] = inv[f]
            for lid, line in inv["lines"].items():
                for f in LINE_FIELDS:
                    out[f"{base}/line/{lid}/{f}"] = line[f]
            out[f"{base}/line-index"] = ",".join(inv["lines"])
            for k, v in self.derived(iid).items():
                out[f"{base}/{k}"] = v
        for vid, ven in self.vendors.items():
            for f in VENDOR_FIELDS:
                out[f"data/vendor/{vid}/{f}"] = ven[f]
        out["data/batch-index"] = ",".join(self.batches)
        for bid, b in self.batches.items():
            out[f"data/batch/{bid}/invoices"] = ",".join(b["invoices"])
            out[f"data/batch/{bid}/total"] = b["total"]
        s = self.summary()
        for c, v in s["category_total"].items():
            out[f"data/summary/category/{c}/total"] = v
        for st, v in s["status_count"].items():
            out[f"data/summary/status/{st}/count"] = v
        return out

    def snapshot(self) -> Ledger:
        return copy.deepcopy(self)

    # ---- lookups ---------------------------------------------------------
    def _invoice(self, iid: str) -> dict[str, Any]:
        if iid not in self.invoices:
            raise LedgerError("not_found", f"no invoice {iid}")
        return self.invoices[iid]

    def _vendor(self, vid: str) -> dict[str, Any]:
        if vid not in self.vendors:
            raise LedgerError("not_found", f"no vendor {vid}")
        return self.vendors[vid]

    def _editable_invoice(self, iid: str) -> dict[str, Any]:
        inv = self._invoice(iid)
        if inv["status"] in LOCKED_STATUSES:
            raise LedgerError("locked", f"{iid} is {inv['status']} and cannot be changed")
        return inv

    # ---- validation ------------------------------------------------------
    def _check_invoice_fields(self, iid: str | None, fields: dict[str, Any]) -> None:
        for k, v in fields.items():
            if k not in INVOICE_FIELDS:
                raise LedgerError("invalid", f"unknown invoice field {k!r}")
            if k == "vendor_id" and v not in self.vendors:
                raise LedgerError("invalid", f"unknown vendor {v!r}")
            if k == "category" and v not in CATEGORIES:
                raise LedgerError("invalid", f"unknown category {v!r}")
            if k == "assignee" and v not in ASSIGNEES:
                raise LedgerError("invalid", f"unknown assignee {v!r}")
            if k == "status":
                if v not in STATUSES:
                    raise LedgerError("invalid", f"unknown status {v!r}")
                if v == "paid":
                    raise LedgerError("invalid", "status 'paid' is set only by a payment batch")
            if k == "discount_cents" and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
                raise LedgerError("invalid", "discount must be a non-negative amount")
            if k in ("date", "due_date", "po_number", "notes") and not isinstance(v, str):
                raise LedgerError("invalid", f"{k} must be text")

    def _check_discount(self, iid: str) -> None:
        inv = self.invoices[iid]
        subtotal = sum(line["qty"] * line["unit_price_cents"] for line in inv["lines"].values())
        if inv["discount_cents"] > subtotal:
            raise LedgerError("invalid", f"discount exceeds subtotal on {iid}")

    @staticmethod
    def _check_line_fields(fields: dict[str, Any]) -> None:
        for k, v in fields.items():
            if k not in LINE_FIELDS:
                raise LedgerError("invalid", f"unknown line field {k!r}")
            if k == "description" and not (isinstance(v, str) and v.strip()):
                raise LedgerError("invalid", "description must be non-empty text")
            if k == "qty" and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
                raise LedgerError("invalid", "qty must be a positive whole number")
            if k == "unit_price_cents" and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
                raise LedgerError("invalid", "unit price must be a non-negative amount")

    # ---- operations (each is all-or-nothing) -------------------------------
    def _atomic(self, fn):
        backup = (
            copy.deepcopy(self.invoices),
            copy.deepcopy(self.vendors),
            copy.deepcopy(self.batches),
            self._next_invoice,
            self._next_batch,
        )
        try:
            return fn()
        except LedgerError:
            self.invoices, self.vendors, self.batches, self._next_invoice, self._next_batch = backup
            raise

    def update_invoice(self, iid: str, fields: dict[str, Any]) -> None:
        def run():
            inv = self._editable_invoice(iid)
            self._check_invoice_fields(iid, fields)
            inv.update(fields)
            self._check_discount(iid)

        self._atomic(run)

    def bulk_update(self, ids: list[str], fields: dict[str, Any]) -> None:
        def run():
            if not ids:
                raise LedgerError("invalid", "no invoices selected")
            for iid in ids:
                inv = self._editable_invoice(iid)
                self._check_invoice_fields(iid, fields)
                inv.update(fields)
                self._check_discount(iid)

        self._atomic(run)

    def create_invoice(self, fields: dict[str, Any]) -> str:
        def run():
            required = ("vendor_id", "date", "category", "assignee")
            missing = [k for k in required if k not in fields]
            if missing:
                raise LedgerError("invalid", f"missing fields {missing}")
            self._check_invoice_fields(None, fields)
            inv = {
                "due_date": fields["date"],
                "po_number": "",
                "notes": "",
                "status": "draft",
                "discount_cents": 0,
                **fields,
                "lines": {},
                "next_line": 1,
            }
            iid = self._new_invoice_id()
            self.invoices[iid] = inv
            return iid

        return self._atomic(run)

    def delete_invoice(self, iid: str) -> None:
        def run():
            inv = self._invoice(iid)
            if inv["status"] != "draft":
                raise LedgerError(
                    "locked", f"only draft invoices can be deleted; {iid} is {inv['status']}"
                )
            del self.invoices[iid]

        self._atomic(run)

    def add_line(self, iid: str, fields: dict[str, Any]) -> str:
        def run():
            inv = self._editable_invoice(iid)
            missing = [k for k in LINE_FIELDS if k not in fields]
            if missing:
                raise LedgerError("invalid", f"missing line fields {missing}")
            self._check_line_fields(fields)
            lid = f"L{inv['next_line']}"
            inv["next_line"] += 1
            inv["lines"][lid] = {k: fields[k] for k in LINE_FIELDS}
            return lid

        return self._atomic(run)

    def update_line(self, iid: str, lid: str, fields: dict[str, Any]) -> None:
        def run():
            inv = self._editable_invoice(iid)
            if lid not in inv["lines"]:
                raise LedgerError("not_found", f"no line {lid} on {iid}")
            self._check_line_fields(fields)
            inv["lines"][lid].update(fields)
            self._check_discount(iid)

        self._atomic(run)

    def remove_line(self, iid: str, lid: str) -> None:
        def run():
            inv = self._editable_invoice(iid)
            if lid not in inv["lines"]:
                raise LedgerError("not_found", f"no line {lid} on {iid}")
            del inv["lines"][lid]
            self._check_discount(iid)

        self._atomic(run)

    def update_vendor(self, vid: str, fields: dict[str, Any]) -> None:
        def run():
            ven = self._vendor(vid)
            for k, v in fields.items():
                if k not in VENDOR_FIELDS:
                    raise LedgerError("invalid", f"unknown vendor field {k!r}")
                if k in ("tax_bp", "early_discount_bp", "terms_days") and (
                    isinstance(v, bool) or not isinstance(v, int) or v < 0
                ):
                    raise LedgerError("invalid", f"{k} must be a non-negative whole number")
                if k == "name" and not (isinstance(v, str) and v.strip()):
                    raise LedgerError("invalid", "name must be non-empty text")
                if k == "contact" and v not in ASSIGNEES:
                    raise LedgerError("invalid", f"unknown contact {v!r}")
            ven.update(fields)

        self._atomic(run)

    def create_batch(self, ids: list[str]) -> str:
        """Pay a set of approved invoices. This is final: paid invoices are locked."""

        def run():
            if not ids:
                raise LedgerError("invalid", "a batch needs at least one invoice")
            if len(set(ids)) != len(ids):
                raise LedgerError("invalid", "duplicate invoices in batch")
            total = 0
            for iid in ids:
                inv = self._invoice(iid)
                if inv["status"] != "approved":
                    raise LedgerError("invalid", f"{iid} is {inv['status']}, not approved")
                total += self.derived(iid)["total"]
            for iid in ids:
                self.invoices[iid]["status"] = "paid"
            bid = f"B-{self._next_batch}"
            self._next_batch += 1
            self.batches[bid] = {"invoices": list(ids), "total": total}
            return bid

        return self._atomic(run)


def diff_flat(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Leaf paths whose value changed, appeared or disappeared."""
    changed = [k for k in after if k not in before or before[k] != after[k]]
    changed += [k for k in before if k not in after]
    return sorted(changed)
