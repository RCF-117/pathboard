import random

import pytest

from pathboard.exp1.simulator import IDEAL, Policy, SimJob, SimStep, lower_bound, simulate
from pathboard.exp2.scoring import score

POLICIES = [IDEAL, Policy("gc", "global", "counter"), Policy("gv", "global", "view"),
            Policy("pv", "path", "view"), Policy("pt", "path", "target")]


def step(o, t, a, page=(), target=(), writes=(), derived=()):
    return SimStep(o, t, a, frozenset(page), frozenset(target), frozenset(writes) | frozenset(derived), frozenset(writes))


A = SimJob("a", (), (step(0, 1, 1, page=["data/x/1"], writes=["data/x/1"]),))
B = SimJob("b", (), (step(0, 1, 1, page=["data/x/2"], writes=["data/x/2"]),))


@pytest.mark.parametrize("policy", POLICIES, ids=lambda p: p.name)
def test_one_worker_is_serial(policy):
    r = simulate((A, B), 1, policy, 0.5, 0.0)
    assert r.makespan_s == 4.0 and r.rechecks == 0 and r.wait_s == 0.0


def test_two_independent_jobs_hand_computed():
    # Both think 0-1 and want to act at 1.
    ms = lambda p, f=0.5: simulate((A, B), 2, p, f, 0.0)  # noqa: E731
    assert ms(IDEAL).makespan_s == 2.0
    assert ms(POLICIES[3]).makespan_s == 2.0 and ms(POLICIES[4]).makespan_s == 2.0  # disjoint paths act together
    gv = ms(POLICIES[2])  # one lock: b waits 1-2, its page did not change, acts 2-3
    assert gv.makespan_s == 3.0 and gv.wait_s == 1.0 and gv.rechecks == 0
    gc = ms(POLICIES[1])  # one lock + counter: b waits 1-2, must look again (0 + 0.5*1), acts 2.5-3.5
    assert gc.makespan_s == 3.5 and gc.rechecks == 1 and gc.recheck_s == 0.5 and gc.wait_s == 1.0
    assert ms(POLICIES[1], 1.0).makespan_s == 4.0  # full re-think: no better than one worker


def test_reader_of_a_written_path_waits_then_looks_again():
    reader = SimJob("b", (), (step(0, 1, 1, page=["data/x"], writes=["data/x/2"]),))
    r = simulate((A, reader), 2, Policy("pv", "path", "view"), 0.5, 0.0)
    assert r.makespan_s == 3.5 and r.rechecks == 1 and r.wait_s == 1.0
    # With reads narrowed to the target, the same pair does not interact.
    assert simulate((A, reader), 2, Policy("pt", "path", "target"), 0.5, 0.0).makespan_s == 2.0


def test_derived_writes_do_not_block_writers_but_do_stale_readers():
    a = SimJob("a", (), (step(0, 1, 1, writes=["data/x/1"], derived=["data/sum"]),))
    b = SimJob("b", (), (step(0, 1, 1, writes=["data/x/2"], derived=["data/sum"]),))
    assert simulate((a, b), 2, Policy("pv", "path", "view"), 0.5, 0.0).makespan_s == 2.0
    watcher = SimJob("w", (), (step(0, 3, 1, page=["data/sum"]),))  # looks at 0, wants to act at 3
    r = simulate((a, watcher), 2, Policy("pv", "path", "view"), 1.0, 0.0)
    assert r.rechecks == 1 and r.makespan_s == 3 + 3 + 1


def test_same_leaf_writers_take_turns():
    b = SimJob("b", (), (step(0, 1, 1, writes=["data/x/1"]),))
    r = simulate((A, b), 2, Policy("pt", "path", "target"), 0.0, 0.0)
    assert r.wait_s == 1.0 and r.rechecks == 1 and r.makespan_s == 3.0


@pytest.mark.parametrize("policy", POLICIES, ids=lambda p: p.name)
def test_strict_chain_gains_nothing(policy):
    jobs = (A, SimJob("b", ("a",), B.steps), SimJob("c", ("b",), A.steps))
    assert simulate(jobs, 4, policy, 0.5, 0.0).makespan_s == 6.0


def test_start_overhead_and_validation():
    assert simulate((A, B), 2, IDEAL, 0.0, 1.5).makespan_s == 3.5
    with pytest.raises(ValueError):
        simulate((A, SimJob("b", ("zzz",), B.steps)), 2, IDEAL, 0.0, 0.0)
    with pytest.raises(RuntimeError):  # dependency cycle
        simulate((SimJob("a", ("b",), A.steps), SimJob("b", ("a",), B.steps)), 2, IDEAL, 0.0, 0.0)
    with pytest.raises(ValueError):
        SimStep(0, 0, 0, frozenset({"view/t/route"}), frozenset(), frozenset(), frozenset())


def test_random_instances_respect_bounds():
    rng = random.Random(1)
    leaves = [f"data/r/{i}/{f}" for i in range(4) for f in "ab"]
    for _ in range(150):
        jobs = []
        for j in range(rng.randint(1, 6)):
            steps = tuple(step(rng.uniform(0, 1), rng.uniform(0, 5), rng.uniform(0, 2),
                               page=rng.sample(leaves, 3) + ["data/r/0"], target=rng.sample(leaves, 1),
                               writes=rng.sample(leaves, rng.randint(0, 2))) for _ in range(rng.randint(1, 5)))
            deps = tuple(f"j{d}" for d in range(j) if rng.random() < 0.25)
            jobs.append(SimJob(f"j{j}", deps, steps))
        jobs = tuple(jobs)
        serial = sum(j.duration for j in jobs)
        for policy in POLICIES:
            assert simulate(jobs, 1, policy, 0.5, 0.0).makespan_s == pytest.approx(serial)
            for w in (2, 3):
                r = simulate(jobs, w, policy, rng.choice((0.0, 0.5, 1.0)), 0.0)
                assert r.makespan_s >= lower_bound(jobs, w, 0.0) - 1e-9
                assert r.actions == sum(len(j.steps) for j in jobs)
                if policy is IDEAL:
                    assert r.rechecks == 0 and r.wait_s == 0.0


# ---- scoring -------------------------------------------------------------------
U = frozenset({"data/i/1/a", "data/i/1/b", "data/i/2/a", "view/t/sort"})


def test_scoring_verdicts():
    truth = frozenset({"data/i/1/a"})
    exact = score(truth, truth, U, U)
    assert exact.verdict == "exact" and exact.safe and exact.recall == exact.precision == 1.0
    wide = score(truth, frozenset({"data/i/1"}), U, U)  # names the whole invoice
    assert wide.verdict == "too_wide" and wide.safe and wide.precision == 0.5
    narrow = score(frozenset({"data/i/1/a", "view/t/sort"}), truth, U, U)
    assert narrow.verdict == "too_narrow" and not narrow.safe and narrow.recall == 0.5
    both = score(truth, frozenset({"data/i/2"}), U, U)
    assert both.verdict == "wide_and_narrow" and both.recall == 0.0 and both.precision == 0.0
    nothing = score(frozenset(), frozenset(), U, U)
    assert nothing.verdict == "exact" and nothing.recall == nothing.precision == 1.0
    phantom = score(truth, frozenset({"data/i/1/a", "data/zzz"}), U, U)
    assert phantom.n_phantom == 1 and phantom.precision == 0.5 and phantom.verdict == "too_wide"
    hidden = score(frozenset({"data/i/1/a", "data/i/2/a"}), truth, U, frozenset({"data/i/1/a"}))
    assert hidden.recall == 0.5 and hidden.visible_recall == 1.0
    with pytest.raises(ValueError):
        score(frozenset({"data/not/in/universe"}), truth, U, U)
