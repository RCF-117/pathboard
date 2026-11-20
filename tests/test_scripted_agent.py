"""The scripted increment agent, think-time sampling, and the move-level checks end to end."""

from __future__ import annotations

import json
import random

import pytest

from conftest import SUITE_KW
from pathboard.agent.scripted import TimedScriptedClient, increment_policy, recorded_think_times
from pathboard.config import from_dict
from pathboard.exp4.run import Condition, Exp4Config, run_one
from pathboard.tasks.families import generate
from test_exp4_live import CFG

GOAL = "TASK\nOn invoice INV-1009, increase the unit price of line L4 by $18.00: add it.\n\nACTIONS SO FAR\n"
ROW = '| L4 | [e29 cell "Design review"] | [e30 cell "9"] | [e31 cell "$1,150.50"] | $1.00 | [e32 button "Remove"] |'
EDITOR = '| L4 | [e29 cell "Design review"] | [e30 cell "9"] | [e31 input "Edit unit price of line L4" value="x"] |'


def prompt(history: str, page: str, notice: bool = False) -> str:
    extra = "\nNOTICE\nThe page changed ...\n" if notice else ""
    return f"{GOAL}{history}{extra}\n\nCURRENT PAGE\n{page}"


def test_policy_reads_the_price_types_it_plus_the_amount_and_saves():
    p = increment_policy()
    a = p(prompt("(none yet)", 'URL: /invoices\n[e3 input "Search" value=""]'))
    assert a["action"] == "fill" and a["text"] == "INV-1009"
    a = p(prompt("1. fill ...", 'URL: /invoices\n| [e7 link "INV-1009"] |'))
    assert a == {"note": "open the invoice", "action": "click", "id": "e7"}
    a = p(prompt("2. click ...", "URL: /invoices/INV-1009\n" + ROW))
    assert a["action"] == "click" and a["id"] == "e31"
    a = p(prompt("3. click e31 -> ok", "URL: /invoices/INV-1009\n" + EDITOR))
    assert a["action"] == "fill" and a["text"] == "1168.50"
    a = p(prompt("4. fill e31 -> ok", "URL: /invoices/INV-1009\n" + EDITOR))
    assert a["action"] == "press" and a["key"] == "Enter"
    assert p(prompt("5. press e31 -> ok", "URL: /invoices/INV-1009\n" + ROW))["action"] == "finish"


def test_policy_starts_over_after_a_notice_or_a_broken_editor():
    p = increment_policy()
    p(prompt("x", "URL: /invoices/INV-1009\n" + ROW))  # reads $1,150.50, opens the editor
    a = p(prompt("2. click -> ok", "URL: /invoices/INV-1009\n" + EDITOR, notice=True))
    assert a["action"] == "press" and a["key"] == "Escape"  # stale draft: close and re-read
    newer = ROW.replace("$1,150.50", "$1,200.00")
    a = p(prompt("3. press -> ok", "URL: /invoices/INV-1009\n" + newer))
    assert a["action"] == "click" and "1218.00" in a["note"]
    a = p(prompt("4. fill e31 -> stale_element: gone", "URL: /invoices/INV-1009\n" + newer))
    assert a["action"] == "click"  # the editor vanished: read the price again


def test_think_times_come_from_recorded_traces_and_are_slept(tmp_path):
    f = tmp_path / "traces.jsonl"
    rows = [
        {"trace": {"task_id": "increment-0", "steps": [{"think_s": 1.5}, {"think_s": 2.5}]}},
        {"trace": {"task_id": "shared_invoice-0", "steps": [{"think_s": 9.0}]}},
    ]
    f.write_text("\n".join(json.dumps(r) for r in rows))
    times = recorded_think_times([str(f)], "increment")
    assert sorted(times) == [1.5, 2.5]
    slept = []
    c = TimedScriptedClient("increment", times, random.Random(1), sleep=slept.append)
    c.decide("s", prompt("(none yet)", 'URL: /invoices\n[e3 input "Search" value=""]'), {})
    assert slept and slept[0] in (1.5, 2.5)
    with pytest.raises(ValueError):
        recorded_think_times([str(f)], "chain")


# Version checks (what the agent's page held against the latest value, as the design doc's read
# marks do) must never lose an update.  Checks that ask only "what changed since my look", from
# the server or from the agent's own page, can: a save made just before the look may not have
# reached the page, so the agent reads a stale price.  Those only have to run cleanly here.
CONDITIONS = [
    (
        Condition(
            "lock_action_move_version", "per_job", "global", "action", "recheck_move_version"
        ),
        True,
    ),
    (Condition("board_move_version", "per_job", "path", "move", "recheck_version"), True),
    (Condition("board_save_version", "per_job", "path", "move", "recheck_move_version"), True),
    (Condition("board_registry", "per_job", "path", "move", "recheck_move_registry"), True),
    (Condition("lock_registry", "per_job", "global", "action", "recheck_move_registry"), True),
    (Condition("board_move_target", "per_job", "path", "move", "recheck_target"), False),
    (
        Condition(
            "lock_action_move_observed", "per_job", "global", "action", "recheck_move_observed"
        ),
        False,
    ),
    (Condition("board_move_observed", "per_job", "path", "move", "recheck_observed"), False),
]


@pytest.mark.browser
@pytest.mark.parametrize("cond, must_be_safe", CONDITIONS, ids=lambda c: getattr(c, "name", ""))
def test_four_scripted_increment_agents(cond, must_be_safe, tmp_path):
    cfg = from_dict(Exp4Config, CFG, "test")
    task = generate("increment", 0, 31, **SUITE_KW)
    row, _traces, _ = run_one(
        cfg,
        task,
        0,
        cond,
        lambda s: TimedScriptedClient("increment", [0.3, 0.8, 1.6], random.Random(s)),
        tmp_path,
    )
    assert row["errors"] == {} and row["all_finished"], row
    if must_be_safe:
        assert row["success"], (row["unmet"], row["collateral"])
    checks = [
        json.loads(line)["check"]
        for f in tmp_path.iterdir()
        for line in f.read_text().splitlines()
        if "check" in json.loads(line)
    ]
    assert checks and all(c["mode"] == cond.notice for c in checks)


@pytest.mark.browser
def test_outside_edits_are_made_and_taken_out_before_judging(tmp_path):
    cfg = from_dict(
        Exp4Config,
        {**CFG, "outside": {"edits": 2, "amount_cents": 700, "first_s": 1.0, "interval_s": 2.0}},
        "test",
    )
    task = generate("increment", 0, 31, **SUITE_KW)
    cond = Condition("board_registry_page", "per_job", "path", "move", "recheck_move_registry_page")
    row, _traces, _ = run_one(
        cfg,
        task,
        0,
        cond,
        lambda s: TimedScriptedClient("increment", [0.3, 0.8, 1.6], random.Random(s)),
        tmp_path,
    )
    assert row["errors"] == {} and row["all_finished"], row
    assert row["outside_added_cents"] == 1400
    assert "outside" in row["writers"]


@pytest.mark.browser
@pytest.mark.parametrize(
    "notice", ["recheck_move_registry", "recheck_move_registry_page"], ids=lambda n: n
)
def test_page_only_board_is_safe_without_server_or_ground_truth(notice, tmp_path):
    """A third-party app: the board's paths, saves and edits come from the agent's page alone."""
    cfg = from_dict(Exp4Config, {**CFG, "page_only": True}, "test")
    task = generate("increment", 0, 31, **SUITE_KW)
    cond = Condition("board_page_only", "per_job", "path", "move", notice)
    row, _traces, _ = run_one(
        cfg,
        task,
        0,
        cond,
        lambda s: TimedScriptedClient("increment", [0.3, 0.8, 1.6], random.Random(s)),
        tmp_path,
    )
    assert row["errors"] == {} and row["all_finished"], row
    assert row["success"], (row["unmet"], row["collateral"])
    assert row["held_moves"] > 0 and row["registry_publishes"] >= 4, row
    saves = [
        json.loads(line)["check"]
        for f in tmp_path.iterdir()
        for line in f.read_text().splitlines()
        if "check" in json.loads(line) and json.loads(line)["check"]["in_move"]
    ]
    assert saves and all(
        any(t.endswith("/unit_price_cents") for t in c["target"]) for c in saves
    ), saves


def test_page_only_refuses_a_check_that_reads_the_server():
    from pathboard.config import ConfigError

    bad = {
        **CFG,
        "page_only": True,
        "conditions": [
            {
                "name": "x",
                "team": "per_job",
                "lock": "path",
                "scope": "move",
                "notice": "recheck_move_version",
            }
        ],
        "baseline": "x",
    }
    cfg = from_dict(Exp4Config, bad, "test")
    from pathboard.exp4.run import _check_config

    with pytest.raises(ConfigError):
        _check_config(cfg, (generate("increment", 0, 31, **SUITE_KW),))
