"""The move window of the save-time version check (``recheck_move_version``)."""

from types import SimpleNamespace

import pytest

from pathboard.agent.checks import ActionCheck
from pathboard.live.registry import Registry

PRICE = "data/invoice/INV-1/line/L4/unit_price_cents"


class _Server:
    def __init__(self):
        self.data = {PRICE: 15050}

    def flat(self):
        return dict(self.data)


class _Probe:
    def __init__(self):
        self.server = _Server()

    def foreign_data_writes_since(self, mark, actor):
        return frozenset()


def _check():
    probe = _Probe()
    check = ActionCheck("recheck_move_version", SimpleNamespace(actor="w1"), probe, None)
    page = {}  # what the agent's page holds
    check._net_flat = lambda: dict(page)
    return check, probe.server, page


def _stop(check, closes_edit=False):
    return check.stop(True, "t", "t", (PRICE,), closes_edit=closes_edit)[0]


def test_a_refused_save_stays_refused_until_the_edit_is_closed():
    check, server, page = _check()
    page[PRICE] = 15050
    check.look("m0")  # the agent reads $150.50
    assert not _stop(check)
    check.acted({"view/T/editing"}, set())  # opens the editor: the move begins
    server.data[PRICE] = 16050  # another agent saves $160.50
    page[PRICE] = 16050  # and the page catches up
    check.look("m1")
    assert _stop(check)  # save refused: the typed value was built on $150.50
    check.look("m2")  # the editor still holds the stale value; the page is fresh
    assert _stop(check)  # pressing save again is refused again
    assert not _stop(check, closes_edit=True)  # but Escape, which saves nothing, is allowed
    check.acted({"view/T/editing"}, set())  # Escape closes the editor
    assert not check.in_move
    check.look("m3")  # reads $160.50 afresh
    assert not _stop(check)


def test_a_save_ends_the_move():
    check, _server, page = _check()
    page[PRICE] = 15050
    check.look("m0")
    check.acted({"view/T/editing"}, set())
    check.look("m1")
    assert not _stop(check)
    check.acted({"view/T/editing"}, {PRICE})
    assert not check.in_move


def test_a_save_on_a_redrawn_editor_is_still_checked():
    check, server, page = _check()
    page[PRICE] = 15050
    check.look("m0")
    assert not _stop(check)  # the click that opens the price's editor targets the price
    check.acted({"view/T/editing"}, set())
    server.data[PRICE] = 19250  # another agent saves
    check.look("m1")
    # the Enter lands on a redrawn editor whose target cannot be resolved
    assert check.stop(True, None, None, (), closes_edit=False)[0]


def _worker(mode, registry, name):
    check = ActionCheck(mode, SimpleNamespace(actor=name), _Probe(), registry)
    page = {PRICE: 15050}
    check._net_flat = lambda: dict(page)
    return check, page


@pytest.mark.parametrize("mode", ["recheck_move_registry", "recheck_move_registry_page"])
def test_registry_is_kept_by_writers_and_refuses_a_stale_save(mode):
    registry = Registry()
    a, page_a = _worker(mode, registry, "w1")
    b, _page_b = _worker(mode, registry, "w2")
    for c in (a, b):  # both read $150.50 and open the editor
        c.look("m0")
        assert not _stop(c)
        c.acted({"view/T/editing"}, set())
    a.look("m1")
    assert not _stop(a)  # a saves first
    page_a[PRICE] = 16850  # its page refreshes right after its save
    a.acted({"view/T/editing"}, {PRICE})
    assert registry.latest((PRICE,)) == {PRICE: 16850}
    b.look("m1")  # b's page has not refreshed yet
    assert _stop(b)  # the registry knows; b's save is refused


def test_registry_misses_an_outside_change_the_page_catches():
    reg_only, page1 = _worker("recheck_move_registry", Registry(), "w1")
    with_page, page2 = _worker("recheck_move_registry_page", Registry(), "w2")
    for c, page in ((reg_only, page1), (with_page, page2)):
        c.look("m0")
        c.acted({"view/T/editing"}, set())
        page[PRICE] = 99900  # someone outside the board saved; the page has refreshed
        c.look("m1")
    assert not _stop(reg_only)
    assert _stop(with_page)


def test_a_registry_mode_needs_a_registry():
    with pytest.raises(ValueError):
        ActionCheck("recheck_move_registry", SimpleNamespace(actor="w1"), _Probe(), None)
    with pytest.raises(ValueError):
        ActionCheck("recheck_move_version", SimpleNamespace(actor="w1"), _Probe(), Registry())
