"""The live board under real threads: who waits for whom, and when holds are released."""

from __future__ import annotations

import threading
import time

import pytest

from pathboard.live.board import Board

CELL_A = ("data/invoice/INV-1/line/L1/qty",)
CELL_B = ("data/invoice/INV-1/line/L2/unit_price_cents",)
OPEN_EDITOR = {"view/T/editing"}
SAVED_A = {"data/invoice/INV-1/line/L1/qty", "data/invoice/INV-1/total"}


def blocked_for(board: Board, worker: str, target, run_after=None, timeout=0.6) -> bool:
    """True if ``worker``'s before_act is still blocked after ``timeout`` seconds."""
    done = threading.Event()

    def go():
        board.before_act(worker, target)
        done.set()

    t = threading.Thread(target=go, daemon=True)
    t.start()
    blocked = not done.wait(timeout)
    if run_after is not None:
        run_after()
        assert done.wait(2.0), "worker never got through after the release"
    t.join(2.0)
    return blocked


def test_no_lock_never_blocks():
    b = Board("none", "action", 30)
    a, w = b.gate("a"), b.gate("b")
    a.before_act("click", CELL_A)
    assert not blocked_for(b, "b", CELL_A)
    w.finish()


def test_global_per_action_blocks_only_during_the_action():
    b = Board("global", "action", 30)
    a, _ = b.gate("a"), b.gate("b")
    a.before_act("click", CELL_A)
    assert blocked_for(b, "b", CELL_B, run_after=lambda: a.after_act(OPEN_EDITOR, set()))


def test_global_per_move_holds_through_the_move():
    b = Board("global", "move", 30)
    a, w = b.gate("a"), b.gate("b")
    a.before_act("click", CELL_A)
    a.after_act(OPEN_EDITOR, set())  # editor open: the move continues, lock kept
    assert blocked_for(b, "b", CELL_B, timeout=0.4)  # b times out still blocked
    # b's thread is still waiting; a saves, which ends the move and lets b in.
    done = threading.Event()
    threading.Thread(target=lambda: (b.before_act("b", CELL_B), done.set()), daemon=True)
    a.before_act("fill", CELL_A)  # inside its own move a never waits
    a.after_act(set(), SAVED_A)
    w.finish()
    b.finish("b")


def test_path_per_move_disjoint_targets_run_together_same_target_waits():
    b = Board("path", "move", 30)
    a, _ = b.gate("a"), b.gate("b")
    a.before_act("click", CELL_A)
    a.after_act(OPEN_EDITOR, set())
    assert not blocked_for(b, "b", CELL_B)  # different line: no wait
    b.finish("b")
    c = b.gate("c")
    assert blocked_for(
        b, "c", CELL_A, run_after=lambda: (a.before_act("press", ()), a.after_act(set(), SAVED_A))
    )
    c.finish()


def test_a_parent_path_conflicts_with_its_children():
    b = Board("path", "action", 30)
    a, _ = b.gate("a"), b.gate("b")
    a.before_act("click", ("data/invoice/INV-1",))  # e.g. the Save button stands for the invoice
    assert blocked_for(b, "b", CELL_A, run_after=lambda: a.after_act(set(), SAVED_A))


def test_navigation_abandons_a_move():
    b = Board("global", "move", 30)
    a, _ = b.gate("a"), b.gate("b")
    a.before_act("click", CELL_A)
    a.after_act(OPEN_EDITOR, set())
    a.before_act("back", ())
    a.after_act({"view/T/route"}, set())
    assert not blocked_for(b, "b", CELL_B)


def test_skipped_action_keeps_a_move_but_releases_a_single_action():
    b = Board("global", "move", 30)
    a, _ = b.gate("a"), b.gate("b")
    a.before_act("click", CELL_A)
    a.skipped()  # not in a move yet: released
    assert not blocked_for(b, "b", CELL_B)


def test_lease_expiry_frees_a_stuck_move():
    b = Board("global", "move", 0.3)
    a, _ = b.gate("a"), b.gate("b")
    a.before_act("click", CELL_A)
    a.after_act(OPEN_EDITOR, set())
    start = time.monotonic()
    b.before_act("b", CELL_B)  # waits until a's lease runs out
    assert 0.25 <= time.monotonic() - start < 2.0
    assert b.stats()["a"].lease_expiries == 1


def test_escalation_inside_a_move_is_counted_not_waited():
    b = Board("path", "move", 30)
    a, w = b.gate("a"), b.gate("b")
    w.before_act("click", CELL_B)
    w.after_act(OPEN_EDITOR, set())
    a.before_act("click", CELL_A)
    a.after_act(OPEN_EDITOR, set())
    a.before_act("click", CELL_B)  # a's move now also wants b's cell: no wait, one escalation
    assert b.stats()["a"].escalations == 1


def test_bad_policy_rejected():
    with pytest.raises(ValueError):
        Board("path", "forever", 30)
