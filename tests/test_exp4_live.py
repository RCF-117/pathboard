"""Experiment 4 end to end with scripted agents: four workers at once on one live app."""

from __future__ import annotations

import pytest

from conftest import SUITE_KW, recategorize_policy
from pathboard.config import from_dict
from pathboard.exp4.run import Condition, Exp4Config, run_one, summarise
from pathboard.llm.base import ScriptedClient
from pathboard.tasks.families import generate

pytestmark = pytest.mark.browser

CFG = {
    "tag": "test",
    "output_root": "unused",
    "suite": {
        "seed": 11,
        "families": ["recategorize"],
        "tasks_per_family": 1,
        "jobs_per_task": 4,
        "n_invoices": 40,
        "n_vendors": 8,
        "max_lines": 4,
        "first_invoice_number": 1001,
    },
    "render_mode": "rerender_keyed",
    "poll_interval_ms": 600,
    "api_latency_ms": 0,
    "page_size": 15,
    "browser": {
        "headless": True,
        "viewport_width": 1400,
        "viewport_height": 900,
        "settle_quiet_ms": 150,
        "settle_timeout_ms": 10000,
        "action_timeout_ms": 5000,
    },
    "handle_mode": "ref",
    "repetitions": 1,
    "order_seed": 1,
    "baseline": "no_board",
    "conditions": [],
    "lease_s": 30.0,
    "start_timeout_s": 60.0,
    "llm": {"provider": "openai", "settings": {}},
    "llm_agent": {"history_actions": 10, "max_steps_whole": 60, "max_steps_job": 20},
    "bootstrap": {"n_boot": 200, "confidence": 0.95, "seed": 1},
    "outside": None,
    "page_only": False,
}

CONDITIONS = [
    Condition("no_board", "per_job", "none", "action", "none"),
    Condition("global_action", "per_job", "global", "action", "recheck_target"),
    Condition("global_move", "per_job", "global", "move", "recheck_target"),
    Condition("path_move", "per_job", "path", "move", "recheck_target"),
    Condition("global_action_observed", "per_job", "global", "action", "recheck_observed"),
    Condition("path_move_observed", "per_job", "path", "move", "recheck_observed"),
]


@pytest.mark.parametrize("cond", CONDITIONS, ids=lambda c: c.name)
def test_four_scripted_workers_at_once(cond):
    cfg = from_dict(Exp4Config, CFG, "test")
    task = generate("recategorize", 0, 11, **SUITE_KW)
    row, traces, stats = run_one(
        cfg, task, 0, cond, lambda _stream: ScriptedClient("rules", recategorize_policy()), None
    )
    assert row["errors"] == {}, row["errors"]
    assert row["success"], (row["unmet"], row["collateral"])
    assert len(traces) == 4 and row["all_finished"]
    assert row["writers"] == ["w1", "w2", "w3", "w4"]  # each worker made its own edit
    assert row["wall_s"] > 0 and set(stats) == {"w1", "w2", "w3", "w4"}
    if cond.lock == "none":
        assert row["lock_waits"] == 0 and row["held_moves"] == 0
    if cond.scope == "move":
        assert row["held_moves"] >= 4  # every inline edit opened an editor first


def test_single_agent_baseline_does_the_whole_task():
    cfg = from_dict(Exp4Config, CFG, "test")
    task = generate("recategorize", 0, 11, **SUITE_KW)
    cond = Condition("one_agent", "one", "none", "action", "none")
    row, traces, _ = run_one(
        cfg, task, 0, cond, lambda _stream: ScriptedClient("rules", recategorize_policy()), None
    )
    assert row["errors"] == {} and row["success"] and row["workers"] == 1
    assert row["writers"] == ["w1"] and traces[0].job_id == "__whole__"


def test_summary_pairs_conditions_with_the_baseline():
    rows = [
        {
            "condition": c,
            "task": t,
            "family": "f",
            "repetition": 0,
            "success": True,
            "collateral": [],
            "errors": {},
            "wall_s": w,
            "n_steps": 4,
            "wrong_target_actions": 0,
            "stale_element_errors": 0,
            "rechecks": 0,
            "lock_wait_s": 0.0,
            "lease_expiries": 0,
            "escalations": 0,
        }
        for t in ("a", "b")
        for c, w in (("no_board", 10.0), ("path_move", 20.0))
    ]
    s = summarise(rows, "no_board", from_dict(Exp4Config, CFG, "t").bootstrap)
    pm = next(c for c in s["conditions"] if c["condition"] == "path_move")
    assert pm["baseline_time_over_this_time"]["estimate"] == pytest.approx(0.5)
    assert pm["success_minus_baseline"]["estimate"] == 0.0
