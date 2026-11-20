"""End-to-end tests through a real headless browser.  Run with:  pytest -m browser"""

import dataclasses

import pytest

from conftest import BROWSER, LEDGER, SUITE_KW, recategorize_policy
from pathboard.browser.executor import ApiActor, SemanticExecutor
from pathboard.browser.groundtruth import GroundTruthProbe
from pathboard.browser.observer import Observer
from pathboard.episodes import LLMAgentSettings, judge, open_tab, run_jobs_oracle, run_whole_llm
from pathboard.exp2.catalog import CATALOG, make_instance
from pathboard.exp3.interferer import Schedule
from pathboard.exp3.run import BootstrapConfig, Condition, Exp3Config, run_one, summarise
from pathboard.llm.base import ScriptedClient
from pathboard.llm.factory import LLMConfig
from pathboard.tasks.families import FAMILIES, generate
from pathboard.tasks.oracle import SyntheticTiming
from pathboard.tasks.suite import SuiteConfig
from pathboard.testbed.server import ServerConfig, TestbedServer
from pathboard.testbed.state import Ledger

pytestmark = pytest.mark.browser
MODES = ("granular", "rerender_keyed", "rerender_anon")
SETTINGS = LLMAgentSettings(history_actions=10, max_steps_whole=60, max_steps_job=20)


def server(mode, ledger=LEDGER, poll=0):
    return TestbedServer(ledger, ServerConfig("127.0.0.1", 0, mode, poll, 0, 15))


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("family", FAMILIES)
def test_oracle_solves_every_family_in_every_render_mode(browser_session, family, mode):
    task = generate(family, 0, 11, **SUITE_KW)
    with server(mode, task.ledger) as srv:
        initial = srv.flat()
        traces = run_jobs_oracle(
            browser_session, srv, task, 0, SyntheticTiming(0.5, 4.0), fresh_tab_per_job=True
        )
        result = judge(task, initial, srv, "agent", task.answer)
        assert result.success, result
        assert [t.job_id for t in traces] == [j.id for j in task.jobs]
        assert all(t.timing_source == "synthetic" for t in traces)
        # The recorded writes account for every data leaf that changed, and match the server's log.
        # (A leaf written twice can end at its old value, so 'recorded' may be larger than the net change.)
        recorded = {w for t in traces for s in t.steps for w in s.writes if w.startswith("data/")}
        final = srv.flat()
        assert recorded >= {k for k in set(initial) | set(final) if initial.get(k) != final.get(k)}
        assert recorded == {w for e in srv.oplog_since(0) for w in e["writes"]}


@pytest.mark.parametrize("mode", MODES)
def test_render_modes_show_the_same_page(browser_session, mode):
    with server(mode) as srv:
        tab = open_tab(browser_session, srv, "agent", "ref")
        text = tab.snapshot().text
        browser_session.close_tab(tab)
    with server("granular") as srv:
        tab = open_tab(browser_session, srv, "agent", "ref")
        assert tab.snapshot().text == text
        browser_session.close_tab(tab)


@pytest.mark.parametrize("mode", MODES)
def test_network_model_matches_server_and_observers_see_an_edit(browser_session, mode):
    with server(mode, poll=400) as srv:
        tab = open_tab(browser_session, srv, "agent", "ref")
        net, dom, anchor = (
            Observer(tab, (s,)) for s in ("network", "model_diff", "mutation_anchor")
        )
        net.begin()
        assert net._net_before == srv.flat()  # independent re-implementation agrees with the server
        dom.begin(), anchor.begin()
        SemanticExecutor(tab).run("inline_edit", invoice="INV-1003", field="notes", value="seen")
        leaf = "data/invoice/INV-1003/notes"
        assert net.end() == frozenset({leaf}) and dom.end() == frozenset({leaf})
        named = anchor.end()
        assert (
            (named == frozenset({leaf})) if mode == "granular" else (len(named) > 1)
        )  # redraws blur the anchor
        # A change by someone else to a row that is not on screen: only the network source sees it.
        net.begin(), dom.begin()
        ApiActor(srv.base_url, "other").call(
            "PATCH", "/api/invoices/INV-1039", {"notes": "far away"}
        )
        tab.gt_wait_applied(srv.seq())
        assert net.end() == frozenset({"data/invoice/INV-1039/notes"}) and dom.end() == frozenset()
        browser_session.close_tab(tab)


def test_every_catalog_action_runs(browser_session):
    led = Ledger(LEDGER)
    for action_type in CATALOG:
        inst = make_instance(action_type, led, 15, 5, 0)
        with server("rerender_keyed", poll=400) as srv:
            tab = open_tab(browser_session, srv, "agent", "ref")
            ex, probe = SemanticExecutor(tab), GroundTruthProbe(tab, srv)
            for op, kw in inst.setup:
                ex.run(op, **kw)
            mark = probe.mark()
            op, kw = inst.action
            ApiActor(srv.base_url, "other").call(**kw) if op == "remote" else ex.run(op, **kw)
            tab.gt_wait_applied(srv.seq())
            assert probe.data_writes_since(mark, None) | probe.view_writes_since(mark), action_type
            browser_session.close_tab(tab)


@pytest.mark.parametrize("handle_mode,expected", [("ref", "stale_element"), ("positional", "ok")])
def test_handles_after_the_rows_move(browser_session, handle_mode, expected):
    """The page is re-sorted between looking and clicking.

    A reference handle fails loudly.  A positional handle clicks whatever now
    sits in that position: the action 'works' but lands on another invoice.
    """
    with server("rerender_keyed") as srv:
        tab = open_tab(browser_session, srv, "agent", handle_mode)
        snap = tab.snapshot()
        handle = next(
            h for h in snap.handles if h.kind == "checkbox" and h.label == "Select INV-1001"
        )
        seen = tab.gt_key_of(handle.id)
        SemanticExecutor(tab).run("sort", column="total")
        now = tab.gt_key_of(handle.id)
        result = tab.act("click", {"id": handle.id})
        assert result.outcome == expected
        assert seen == "row-check:INV-1001"
        if handle_mode == "positional":
            assert now is not None and now != seen and now.startswith("row-check:INV-")
        else:
            assert now is None
        browser_session.close_tab(tab)


def test_agent_loop_with_scripted_model(browser_session):
    task = generate("recategorize", 0, 11, **SUITE_KW)
    with server("rerender_keyed", task.ledger) as srv:
        initial = srv.flat()
        trace = run_whole_llm(
            browser_session,
            srv,
            task,
            0,
            ScriptedClient("rules", recategorize_policy()),
            SETTINGS,
            "ref",
            "none",
            None,
            None,
            None,
        )
        assert judge(task, initial, srv, "agent", None).success
        assert (
            trace.finished
            and trace.timing_source == "measured"
            and trace.steps[-1].outcome == "finish"
        )
        edits = [s for s in trace.steps if s.action == "select"]
        assert len(edits) == len(task.jobs)
        for s in edits:  # each edit wrote exactly one stored leaf, the one the element stands for
            stored = [w for w in s.writes if w.startswith("data/invoice/") and w.count("/") == 3]
            assert len(stored) == 1 and s.reads_target == (stored[0],) and not s.wrong_target
        assert all(s.observe_s >= 0 and s.think_s >= 0 and s.act_s >= 0 for s in trace.steps)


def _exp3_cfg(conditions):
    return Exp3Config(
        tag="t",
        output_root="unused",
        suite=SuiteConfig(11, ("recategorize",), 1, 4, 40, 8, 4, 1001),
        render_mode="rerender_keyed",
        poll_interval_ms=400,
        api_latency_ms=0,
        page_size=15,
        browser=BROWSER,
        repetitions=1,
        baseline="base",
        conditions=conditions,
        schedule=Schedule(1, 2, 3),
        interferer_seed=1,
        llm=LLMConfig("anthropic", {}),
        llm_agent=SETTINGS,
        bootstrap=BootstrapConfig(200, 0.95, 0),
    )


def _tick_policy(invoice):
    """Tick one invoice's checkbox, then finish.  Stateless, so it re-decides after a look-again notice."""

    def policy(prompt: str) -> dict:
        import re

        page = prompt.split("CURRENT PAGE")[1]
        box = re.search(rf'\[(e\d+) checkbox "Select {invoice}" unchecked\]', page)
        return (
            {"note": "tick", "action": "click", "id": box.group(1)}
            if box
            else {"note": "done", "action": "finish"}
        )

    return policy


def test_interference_conditions(browser_session):
    task = generate("recategorize", 0, 11, **SUITE_KW)
    # (1) A real task while unrelated rows change: redraws make reference handles stale, the agent
    #     retries, and the task still succeeds.
    conds = (
        Condition("base", "none", "stale_window", "none", "ref"),
        Condition("other_rows", "data_other_rows", "stale_window", "none", "ref"),
    )
    cfg = _exp3_cfg(conds)
    rows = {
        c.name: run_one(
            browser_session, cfg, task, 0, c, ScriptedClient("rules", recategorize_policy()), None
        )[0]
        for c in conds
    }
    assert rows["base"]["success"] and rows["base"]["events_fired"] == 0
    assert rows["other_rows"]["events_fired"] == 3 and rows["other_rows"]["success"]
    assert (
        rows["other_rows"]["stale_element_errors"] >= 1
        and rows["other_rows"]["wrong_target_actions"] == 0
    )
    summary = summarise(list(rows.values()), "base", cfg.bootstrap)
    assert {c["condition"] for c in summary["conditions"]} == set(rows)
    assert all(
        "success_minus_baseline" in c for c in summary["conditions"] if c["condition"] != "base"
    )

    # (2) The list is re-sorted between the agent looking and clicking a row's checkbox.
    first = sorted(Ledger(task.ledger).invoices, key=lambda i: int(i[4:]))[0]
    resort = (
        Condition("pos", "view_same_tab", "stale_window", "none", "positional"),
        Condition("ref", "view_same_tab", "stale_window", "none", "ref"),
        Condition("pos_told", "view_same_tab", "stale_window", "recheck_target", "positional"),
        Condition("pos_seen", "view_same_tab", "before_observe", "none", "positional"),
    )
    cfg = dataclasses.replace(_exp3_cfg(resort), baseline="pos", schedule=Schedule(0, 1, 1))
    got = {
        c.name: run_one(
            browser_session, cfg, task, 0, c, ScriptedClient("tick", _tick_policy(first)), None
        )
        for c in resort
    }
    ticked = lambda name: [s for s in got[name][1].steps if s.outcome == "ok"]  # noqa: E731
    # Positional handle: the click "works" but lands on another invoice's checkbox.
    assert got["pos"][0]["wrong_target_actions"] == 1
    assert (
        ticked("pos")[0].intended_target
        == (f"row-check:{first}",)
        != ticked("pos")[0].actual_target
    )
    # Reference handle: the stale element fails loudly instead; the retry hits the right one.
    assert got["ref"][0]["wrong_target_actions"] == 0 and got["ref"][0]["stale_element_errors"] == 1
    # Told to look again: no wrong click, one recheck.
    assert got["pos_told"][0]["wrong_target_actions"] == 0 and got["pos_told"][0]["rechecks"] == 1
    assert ticked("pos_told")[0].actual_target == (f"row-check:{first}",)
    # Same change, but before the agent looks: nothing goes wrong.
    assert got["pos_seen"][0]["wrong_target_actions"] == 0 and got["pos_seen"][0]["rechecks"] == 0


def test_agent_sees_its_earlier_notes(browser_session):
    """The agent's notes are its only memory of pages it left: they must reach later prompts."""
    prompts: list[str] = []
    replies = iter(
        [
            {"note": "Vendors page: Globex early-payment rate is 1.5%.", "action": "wait"},
            {"note": "done", "action": "finish"},
        ]
    )

    def policy(prompt: str) -> dict:
        prompts.append(prompt)
        return next(replies)

    task = generate("recategorize", 0, 11, **SUITE_KW)
    with server("rerender_keyed", task.ledger) as srv:
        run_whole_llm(
            browser_session,
            srv,
            task,
            0,
            ScriptedClient("notes", policy),
            SETTINGS,
            "ref",
            "none",
            None,
            None,
            None,
        )
    history = prompts[1].split("ACTIONS SO FAR")[1].split("CURRENT PAGE")[0]
    assert "1. wait -> " in history
    assert "note: Vendors page: Globex early-payment rate is 1.5%." in history
