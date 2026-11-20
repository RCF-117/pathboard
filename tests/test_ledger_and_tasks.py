import json
import urllib.error
import urllib.request

import pytest

from conftest import LEDGER, SUITE_KW
from pathboard.tasks.families import FAMILIES, generate
from pathboard.tasks.model import Expectation, check, is_derived
from pathboard.testbed.server import ServerConfig, TestbedServer
from pathboard.testbed.state import Ledger, LedgerError, diff_flat


def test_generation_is_deterministic():
    assert Ledger(LEDGER).flat() == Ledger(LEDGER).flat()


def test_totals_are_exact():
    led = Ledger(LEDGER)
    for iid, inv in led.invoices.items():
        d = led.derived(iid)
        subtotal = sum(line["qty"] * line["unit_price_cents"] for line in inv["lines"].values())
        rate = led.vendors[inv["vendor_id"]]["tax_bp"]
        assert d["subtotal"] == subtotal
        assert d["tax"] == round((subtotal - inv["discount_cents"]) * rate / 10000 + 1e-9)
        assert d["total"] == subtotal - inv["discount_cents"] + d["tax"]


def test_vendor_tax_changes_exactly_its_invoices():
    led = Ledger(LEDGER)
    before = led.flat()
    vid = next(iter(led.vendors))
    led.update_vendor(vid, {"tax_bp": led.vendors[vid]["tax_bp"] + 100})
    changed = diff_flat(before, led.flat())
    mine = {i for i, inv in led.invoices.items() if inv["vendor_id"] == vid}
    touched = {p.split("/")[2] for p in changed if p.startswith("data/invoice/")}
    assert touched == {i for i in mine if led.derived(i)["subtotal"] > 0}
    assert all(is_derived(p) or p == f"data/vendor/{vid}/tax_bp" for p in changed)


def test_rejected_operation_changes_nothing():
    led = Ledger(LEDGER)
    before = led.flat()
    ids = list(led.invoices)[:3]
    with pytest.raises(LedgerError):
        led.bulk_update(ids, {"category": "NotACategory"})
    with pytest.raises(LedgerError):
        led.bulk_update(ids + ["INV-0"], {"category": "Legal"})  # third id is fine, fourth unknown
    with pytest.raises(LedgerError):
        led.update_invoice(ids[0], {"discount_cents": 10**12})
    assert led.flat() == before


def test_payment_is_final():
    led = Ledger(LEDGER)
    iid = next(i for i, inv in led.invoices.items() if inv["status"] == "approved")
    led.create_batch([iid])
    assert led.invoices[iid]["status"] == "paid"
    for attempt in (
        lambda: led.update_invoice(iid, {"notes": "x"}),
        lambda: led.create_batch([iid]),
        lambda: led.delete_invoice(iid),
    ):
        with pytest.raises(LedgerError):
            attempt()


def test_server_logs_ground_truth_and_rejects_bad_input():
    with TestbedServer(LEDGER, ServerConfig("127.0.0.1", 0, "granular", 0, 0, 15)) as srv:

        def call(method, path, body):
            req = urllib.request.Request(
                srv.base_url + path,
                data=json.dumps(body).encode(),
                method=method,
                headers={"X-Actor": "tester", "Content-Type": "application/json"},
            )
            return json.loads(urllib.request.urlopen(req, timeout=5).read())

        before = srv.flat()
        call("PATCH", "/api/invoices/INV-1001", {"notes": "hello"})
        (entry,) = srv.oplog_since(0)
        assert entry["actor"] == "tester" and entry["writes"] == ["data/invoice/INV-1001/notes"]
        assert entry["writes"] == diff_flat(before, srv.flat())
        with pytest.raises(urllib.error.HTTPError) as err:
            call("PATCH", "/api/invoices/INV-1001", {"category": "Nope"})
        assert err.value.code == 400 and srv.seq() == 1  # rejected calls are not logged
        changes = json.loads(
            urllib.request.urlopen(srv.base_url + "/api/changes?since=0", timeout=5).read()
        )
        assert [r["id"] for r in changes["invoices"]] == ["INV-1001"]


@pytest.mark.parametrize("family", FAMILIES)
def test_task_generation_and_checker(family):
    task = generate(family, 0, 11, **SUITE_KW)
    assert task == generate(family, 0, 11, **SUITE_KW)  # same arguments, same task
    assert task != generate(family, 1, 11, **SUITE_KW)
    led = Ledger(task.ledger)
    initial = led.flat()
    # Doing nothing fails; the expectations name only stored leaves.
    assert not check(task, initial, initial, frozenset(), task.answer).success
    assert not any(is_derived(e.path) for e in task.expectations)
    # Build the intended final state directly and confirm the checker accepts exactly it.
    final = dict(initial)
    for e in task.expectations:
        final[e.path] = e.expected(final) if e.kind != "equals" else e.value
    ok = check(task, initial, final, frozenset(), task.answer)
    assert ok.success and ok.unmet == () and ok.collateral == ()
    # An extra change the task did not ask for is collateral damage ...
    stray = next(
        p
        for p in initial
        if p.endswith("/po_number") and p not in {e.path for e in task.expectations}
    )
    damaged = {**final, stray: "PO-XXXXX"}
    res = check(task, initial, damaged, frozenset(), task.answer)
    assert not res.success and res.collateral == (stray,)
    # ... unless someone else made it.
    assert check(task, initial, damaged, frozenset({stray}), task.answer).success
    if task.answer is not None:
        assert not check(task, initial, final, frozenset(), "INV-0000").success
        assert not check(task, initial, final, frozenset(), f"{task.answer} or INV-0000").success
        assert not check(task, initial, final, frozenset(), None).success


def test_computed_expectation_follows_final_state():
    task = generate("early_discount", 0, 11, **SUITE_KW)
    exp = next(e for e in task.expectations if e.kind == "early_discount")
    flat = Ledger(task.ledger).flat()
    base = exp.path.rsplit("/", 1)[0]
    vendor = flat[f"{base}/vendor_id"]
    first = exp.expected(flat)
    flat[f"data/vendor/{vendor}/early_discount_bp"] += 100
    rate = flat[f"data/vendor/{vendor}/early_discount_bp"]
    assert exp.expected(flat) == (2 * flat[f"{base}/subtotal"] * rate + 10000) // 20000 > first
    assert Expectation("equals", "x", 3).expected({}) == 3
