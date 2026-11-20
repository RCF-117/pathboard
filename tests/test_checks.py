"""The move window of the save-time version check (``recheck_move_version``)."""

from types import SimpleNamespace

from pathboard.agent.checks import ActionCheck

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
    check = ActionCheck("recheck_move_version", SimpleNamespace(actor="w1"), probe)
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
