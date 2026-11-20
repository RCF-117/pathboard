"""Seeded task families.

Each generator picks targets from a freshly generated ledger, writes the
instructions, an authored job decomposition (so planner quality is held
fixed), and an oracle program per job.  The expected final state is derived by
applying the intended operations to a copy of the ledger, so the expectations
cover every stored leaf the task should change and nothing else.

Families and the dependency structure they exercise:

  A recategorize   independent single-field edits on the invoice list
  B line_fix       independent multi-step edits on invoice detail pages
  C early_discount look up a rate on one page, compute, edit on another
  D approve_pay    independent approvals, then one final job that needs them all
  E chain          strictly sequential jobs (each needs the previous one)
  F find_edit      view-wide sorting/filtering mixed with plain edits; has an answer
"""

from __future__ import annotations

import random
from typing import Any, Callable

from ..testbed.state import ASSIGNEES, CATEGORIES, Ledger, LedgerConfig, diff_flat
from .model import Expectation, Job, Task, is_derived

FAMILIES: tuple[str, ...] = ("recategorize", "line_fix", "early_discount", "approve_pay", "chain", "find_edit")

Program = tuple[tuple[str, dict[str, Any]], ...]


def _dollars(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _editable(led: Ledger) -> list[str]:
    return [i for i, inv in led.invoices.items() if inv["status"] in ("draft", "review", "approved")]


def _expectations(before: Ledger, after: Ledger, computed: dict[str, str]) -> tuple[Expectation, ...]:
    """Every stored leaf that differs, as an expectation. ``computed`` maps path -> kind."""
    out = []
    after_flat = after.flat()
    for path in diff_flat(before.flat(), after_flat):
        if is_derived(path):
            continue
        if path in computed:
            out.append(Expectation(computed[path], path, None))
        else:
            if path not in after_flat:
                raise ValueError(f"task would remove leaf {path}; removal expectations are not supported")
            out.append(Expectation("equals", path, after_flat[path]))
    missing = [p for p in computed if p not in {e.path for e in out}]
    for path in missing:  # computed value happens to equal the initial one
        out.append(Expectation(computed[path], path, None))
    return tuple(sorted(out, key=lambda e: e.path))


def _whole_goal(jobs: list[Job], tail: str) -> str:
    lines = ["Complete all of the following in the Ledger app:"]
    lines += [f"{n}. {j.goal}" for n, j in enumerate(jobs, start=1)]
    if tail:
        lines.append(tail)
    return "\n".join(lines)


def _find_and_edit(iid: str, field: str, value: str) -> Program:
    return (("reset_view", {}), ("search", {"text": iid}),
            ("inline_edit", {"invoice": iid, "field": field, "value": value}))


# ---- A ---------------------------------------------------------------------
def recategorize(task_id: str, cfg: LedgerConfig, rng: random.Random, n_jobs: int) -> Task:
    led = Ledger(cfg)
    ref = led.snapshot()
    jobs = []
    for n, iid in enumerate(rng.sample(_editable(led), n_jobs), start=1):
        field = rng.choice(("category", "assignee"))
        pool = CATEGORIES if field == "category" else ASSIGNEES
        value = rng.choice([v for v in pool if v != led.invoices[iid][field]])
        ref.update_invoice(iid, {field: value})
        jobs.append(Job(f"j{n}", f"Set the {field} of invoice {iid} to {value}.", (), "cheap",
                        _find_and_edit(iid, field, value)))
    return Task(task_id, "recategorize", cfg, _whole_goal(jobs, ""), tuple(jobs), _expectations(led, ref, {}), None, None)


# ---- B ---------------------------------------------------------------------
def line_fix(task_id: str, cfg: LedgerConfig, rng: random.Random, n_jobs: int) -> Task:
    led = Ledger(cfg)
    ref = led.snapshot()
    jobs = []
    for n, iid in enumerate(rng.sample(_editable(led), n_jobs), start=1):
        lines = list(led.invoices[iid]["lines"])
        l_qty, l_price = lines[0], lines[-1]
        qty = rng.choice([q for q in range(1, 13) if q != led.invoices[iid]["lines"][l_qty]["qty"]])
        price = rng.randint(20, 900) * 100 + rng.choice((0, 25, 50, 75))
        if price == led.invoices[iid]["lines"][l_price]["unit_price_cents"]:
            price += 100
        note = f"corrected {rng.randint(100, 999)}"
        ref.update_line(iid, l_qty, {"qty": qty})
        ref.update_line(iid, l_price, {"unit_price_cents": price})
        ref.update_invoice(iid, {"notes": note})
        goal = (f"On invoice {iid}: change the quantity of line {l_qty} to {qty}, change the unit price of line "
                f"{l_price} to ${_dollars(price)}, and set the notes to \"{note}\".")
        program: Program = (("reset_view", {}), ("search", {"text": iid}), ("open_invoice", {"invoice": iid}),
                            ("line_edit", {"line": l_qty, "field": "qty", "value": str(qty)}),
                            ("line_edit", {"line": l_price, "field": "unit_price_cents", "value": _dollars(price)}),
                            ("detail_set", {"field": "notes", "value": note}), ("detail_save", {}))
        jobs.append(Job(f"j{n}", goal, (), "cheap", program))
    return Task(task_id, "line_fix", cfg, _whole_goal(jobs, ""), tuple(jobs), _expectations(led, ref, {}), None, None)


# ---- C ---------------------------------------------------------------------
def early_discount(task_id: str, cfg: LedgerConfig, rng: random.Random, n_jobs: int) -> Task:
    led = Ledger(cfg)
    ref = led.snapshot()
    jobs, computed = [], {}
    for n, iid in enumerate(rng.sample(_editable(led), n_jobs), start=1):
        vendor = led.invoices[iid]["vendor_id"]
        rate_bp = led.vendors[vendor]["early_discount_bp"]
        subtotal = led.derived(iid)["subtotal"]
        discount = (2 * subtotal * rate_bp + 10000) // 20000  # half up
        ref.update_invoice(iid, {"discount_cents": discount})
        computed[f"data/invoice/{iid}/discount_cents"] = "early_discount"
        goal = (f"Set the discount of invoice {iid} to its vendor's early-payment discount rate applied to the "
                f"invoice subtotal, rounded to the nearest cent (half up). Rates are listed on the Vendors page.")
        program: Program = (("reset_view", {}), ("search", {"text": iid}), ("goto", {"page": "vendors"}),
                            ("goto", {"page": "invoices"}), ("open_invoice", {"invoice": iid}),
                            ("detail_set", {"field": "discount", "value": _dollars(discount)}), ("detail_save", {}))
        jobs.append(Job(f"j{n}", goal, (), "cheap", program))
    return Task(task_id, "early_discount", cfg, _whole_goal(jobs, ""), tuple(jobs),
                _expectations(led, ref, computed), None, None)


# ---- D ---------------------------------------------------------------------
def approve_pay(task_id: str, cfg: LedgerConfig, rng: random.Random, n_jobs: int) -> Task:
    led = Ledger(cfg)
    ref = led.snapshot()
    review = [i for i, inv in led.invoices.items() if inv["status"] == "review"]
    if len(review) < n_jobs - 1:
        raise ValueError(f"{task_id}: ledger has only {len(review)} invoices in review, need {n_jobs - 1}")
    picked = sorted(rng.sample(review, n_jobs - 1), key=lambda i: int(i[4:]))
    jobs = []
    for n, iid in enumerate(picked, start=1):
        ref.update_invoice(iid, {"status": "approved"})
        jobs.append(Job(f"j{n}", f"Set the status of invoice {iid} to approved.", (), "cheap",
                        _find_and_edit(iid, "status", "approved")))
    ref.create_batch(picked)
    pay_goal = (f"After those invoices are approved, create ONE payment batch on the Payments page containing "
                f"exactly these invoices and no others: {', '.join(picked)}.")
    jobs.append(Job(f"j{n_jobs}", pay_goal, tuple(j.id for j in jobs), "final",
                    (("goto", {"page": "payments"}), ("pay", {"invoices": picked}))))
    return Task(task_id, "approve_pay", cfg, _whole_goal(jobs, ""), tuple(jobs), _expectations(led, ref, {}), None, None)


# ---- E ---------------------------------------------------------------------
def chain(task_id: str, cfg: LedgerConfig, rng: random.Random, n_jobs: int) -> Task:
    if n_jobs < 3:
        raise ValueError("chain needs at least 3 jobs")
    led = Ledger(cfg)
    ref = led.snapshot()
    vendor = rng.choice(list(led.vendors))
    date = f"2026-{rng.randint(10, 12)}-{rng.randint(10, 28)}"
    category, assignee = rng.choice(CATEGORIES), rng.choice(ASSIGNEES)
    new_id = ref.create_invoice({"vendor_id": vendor, "date": date, "category": category, "assignee": assignee})
    jobs = [Job("j1", f"Create a new invoice for vendor {led.vendors[vendor]['name']} dated {date} with category "
                      f"{category} and assignee {assignee}. It will get the id {new_id}.", (), "expensive",
                (("reset_view", {}), ("new_invoice", {"vendor_id": vendor, "date": date, "category": category,
                                                       "assignee": assignee})))]
    open_new: Program = (("reset_view", {}), ("search", {"text": new_id}), ("open_invoice", {"invoice": new_id}))
    for n in range(2, n_jobs):
        desc, qty = f"Item {rng.randint(100, 999)}", rng.randint(1, 9)
        price = rng.randint(20, 400) * 100 + rng.choice((0, 50))
        ref.add_line(new_id, {"description": desc, "qty": qty, "unit_price_cents": price})
        jobs.append(Job(f"j{n}", f"Once invoice {new_id} exists and has {n - 2} line item(s), add a line item to it: "
                                 f"description \"{desc}\", quantity {qty}, unit price ${_dollars(price)}.",
                        (f"j{n - 1}",), "expensive",
                        open_new + (("line_add", {"description": desc, "qty": str(qty), "unit_price": _dollars(price)}),)))
    po = f"PO-{rng.randint(10000, 99999)}"
    ref.update_invoice(new_id, {"po_number": po, "status": "review"})
    jobs.append(Job(f"j{n_jobs}", f"Once invoice {new_id} has all {n_jobs - 2} line item(s), set its PO number to "
                                  f"{po} and its status to review.", (f"j{n_jobs - 1}",), "cheap",
                    open_new + (("detail_set", {"field": "po_number", "value": po}),
                                ("detail_set", {"field": "status", "value": "review"}), ("detail_save", {}))))
    return Task(task_id, "chain", cfg, _whole_goal(jobs, ""), tuple(jobs), _expectations(led, ref, {}), None, None)


# ---- F ---------------------------------------------------------------------
def find_edit(task_id: str, cfg: LedgerConfig, rng: random.Random, n_jobs: int) -> Task:
    if n_jobs < 3:
        raise ValueError("find_edit needs at least 3 jobs")
    led = Ledger(cfg)
    ref = led.snapshot()
    review = [i for i, inv in led.invoices.items() if inv["status"] == "review"]
    totals = sorted(((led.derived(i)["total"], i) for i in review), reverse=True)
    if len(totals) < 2 or totals[0][0] == totals[1][0]:
        raise ValueError(f"{task_id}: no unique highest-total invoice in review")
    top = totals[0][1]
    ref.update_invoice(top, {"notes": "priority"})
    jobs = [Job("j1", "Find the invoice with the highest total among invoices whose status is review, and set its "
                      "notes to \"priority\".", (), "cheap",
                (("reset_view", {}), ("filter_status", {"status": "review"}), ("sort", {"column": "total"}),
                 ("sort", {"column": "total"}), ("inline_edit", {"invoice": top, "field": "notes", "value": "priority"})))]
    # Earliest-dated editable invoice of some assignee, unique, and not the invoice above.
    candidates = []
    for person in ASSIGNEES:
        mine = sorted((led.invoices[i]["date"], i) for i in _editable(led) if led.invoices[i]["assignee"] == person)
        if len(mine) >= 2 and mine[0][0] != mine[1][0] and mine[0][1] != top:
            candidates.append((person, mine[0][1]))
    if not candidates:
        raise ValueError(f"{task_id}: no assignee with a unique earliest invoice")
    person, earliest = rng.choice(candidates)
    category = rng.choice([c for c in CATEGORIES if c != led.invoices[earliest]["category"]])
    ref.update_invoice(earliest, {"category": category})
    jobs.append(Job("j2", f"Find the earliest-dated invoice assigned to {person} (ignore paid and void invoices) "
                          f"and set its category to {category}.", (), "cheap",
                    (("reset_view", {}), ("sort", {"column": "date"}), ("search", {"text": earliest}),
                     ("inline_edit", {"invoice": earliest, "field": "category", "value": category}))))
    others = [i for i in _editable(led) if i not in (top, earliest)]
    for n, iid in enumerate(rng.sample(others, n_jobs - 2), start=3):
        note = f"reviewed {rng.randint(100, 999)}"
        ref.update_invoice(iid, {"notes": note})
        jobs.append(Job(f"j{n}", f"Set the notes of invoice {iid} to \"{note}\".", (), "cheap",
                        _find_and_edit(iid, "notes", note)))
    tail = "When everything is done, give the id of the invoice from item 1 as your final answer."
    return Task(task_id, "find_edit", cfg, _whole_goal(jobs, tail), tuple(jobs), _expectations(led, ref, {}), top, "j1")


_GENERATORS: dict[str, Callable[[str, LedgerConfig, random.Random, int], Task]] = {
    "recategorize": recategorize, "line_fix": line_fix, "early_discount": early_discount,
    "approve_pay": approve_pay, "chain": chain, "find_edit": find_edit,
}


def generate(family: str, index: int, seed: int, n_jobs: int, n_invoices: int, n_vendors: int, max_lines: int,
             first_invoice_number: int) -> Task:
    """Build task ``<family>-<index>``.  Its ledger and targets depend only on the arguments."""
    if family not in _GENERATORS:
        raise KeyError(f"unknown task family {family!r}; choose from {FAMILIES}")
    # String seeds are hashed with SHA-512 by random.Random, so this is stable across runs.
    task_seed = random.Random(f"{seed}/{family}/{index}").getrandbits(31)
    cfg = LedgerConfig(seed=task_seed, n_invoices=n_invoices, n_vendors=n_vendors, max_lines=max_lines,
                       first_invoice_number=first_invoice_number)
    return _GENERATORS[family](f"{family}-{index}", cfg, random.Random(task_seed), n_jobs)
