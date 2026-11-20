"""Experiment 1, step 2: replay recorded traces with several workers and summarise.

Reads a run directory written by ``exp1.collect``, simulates every task under
every policy, worker count and recheck cost in the config, and writes

  sim_rows.jsonl   one row per (task, repetition, policy, workers, recheck cost)
  summary.json     aggregated speedups, headroom ratios and interference rates
  speedup.png      speedup over one worker, by policy and worker count

Usage:  python -m pathboard.exp1.simulate CONFIG.yaml
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..config import ConfigError, load
from ..episodes import trace_seconds
from ..paths import any_conflict
from ..plots import line_chart
from ..runlog import append_jsonl, create_run_dir, read_jsonl, write_json, write_manifest
from ..tasks.model import is_derived
from ..stats import cluster_bootstrap_ci, geometric_mean
from ..trace import EpisodeTrace
from .simulator import IDEAL, LockRule, Policy, SimJob, SimStep, StaleRule, lower_bound, simulate


@dataclass(frozen=True)
class PolicySpec:
    name: str
    lock: LockRule
    stale: StaleRule


@dataclass(frozen=True)
class HeadroomPair:
    baseline: str  # policy name
    candidate: str  # policy name


@dataclass(frozen=True)
class BootstrapConfig:
    n_boot: int
    confidence: float
    seed: int


@dataclass(frozen=True)
class SimulateConfig:
    tag: str
    output_root: str
    collect_run: str  # run directory of exp1.collect
    workers: tuple[int, ...]
    policies: tuple[PolicySpec, ...]
    recheck_think_fractions: tuple[float, ...]
    job_start_overhead_s: float
    headroom: tuple[HeadroomPair, ...]
    plot_recheck_think_fraction: float  # which recheck cost the figure shows
    only_successful_runs: bool  # replay only decomposed runs whose final state passed the checker
    allow_synthetic_timing: bool  # must be true to analyse oracle traces (pipeline tests only)
    bootstrap: BootstrapConfig


def _data(paths) -> frozenset[str]:
    return frozenset(p for p in paths if p.startswith("data/"))


def to_sim_job(trace: EpisodeTrace, deps: tuple[str, ...]) -> SimJob:
    steps = tuple(
        SimStep(
            s.observe_s,
            s.think_s,
            s.act_s,
            _data(s.reads_view),
            _data(s.reads_target),
            _data(s.writes),
            frozenset(p for p in _data(s.writes) if not is_derived(p)),
        )
        for s in trace.steps
    )
    return SimJob(trace.job_id, deps, steps)


def interference(jobs: tuple[SimJob, ...]) -> dict[str, Any]:
    """Static overlap between pairs of jobs that are not ordered by a dependency."""
    by_id = {j.id: j for j in jobs}

    def ancestors(jid: str) -> set[str]:
        out: set[str] = set()
        stack = list(by_id[jid].deps)
        while stack:
            d = stack.pop()
            if d not in out:
                out.add(d)
                stack.extend(by_id[d].deps)
        return out

    agg = {
        j.id: (
            frozenset().union(*(s.holds for s in j.steps)),
            frozenset().union(*(s.writes for s in j.steps)),
            frozenset().union(*(s.reads_view for s in j.steps)),
            frozenset().union(*(s.reads_target for s in j.steps)),
        )
        for j in jobs
    }
    pairs = ww = rw_view = rw_target = 0
    ids = [j.id for j in jobs]
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            if a in ancestors(b) or b in ancestors(a):
                continue
            pairs += 1
            ha, wa, va, ta = agg[a]
            hb, wb, vb, tb = agg[b]
            ww += any_conflict(ha, hb)
            rw_view += any_conflict(wa, vb) or any_conflict(wb, va)
            rw_target += any_conflict(wa, tb) or any_conflict(wb, ta)
    return {
        "unordered_pairs": pairs,
        "write_write": ww,
        "write_vs_page_read": rw_view,
        "write_vs_target_read": rw_target,
    }


def _gmean_ci(
    by_task: dict[str, list[float]], boot: BootstrapConfig, rng: np.random.Generator
) -> dict[str, float]:
    est, low, high = cluster_bootstrap_ci(
        by_task, geometric_mean, boot.n_boot, boot.confidence, rng
    )
    return {"estimate": est, "low": low, "high": high, "n_tasks": len(by_task)}


@dataclass
class CollectRun:
    """What ``exp1.collect`` wrote, indexed for replay."""

    tasks: dict[str, dict[str, Any]]
    results: dict[tuple[str, int, str], dict[str, Any]]  # (task, repetition, mode) -> result row
    traces: dict[
        tuple[str, int], dict[str, EpisodeTrace]
    ]  # decomposed: (task, rep) -> job -> trace
    whole_seconds: dict[tuple[str, int], float]  # monolithic: (task, rep) -> total seconds
    timing_sources: set[str]


@dataclass
class Replay:
    """Accumulated simulation results over all usable runs."""

    used: int = 0
    skipped_failed: int = 0
    skipped_incomplete: int = 0
    # (policy, workers, recheck fraction) -> task -> speedups
    speedup: dict[tuple[str, int, float], dict[str, list[float]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(list))
    )
    # (task, rep, recheck fraction, policy, workers) -> makespan
    makespan: dict[tuple[str, int, float, str, int], float] = field(default_factory=dict)
    decomposition_cost: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    overlap_by_family: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(int))
    )


def _policies(cfg: SimulateConfig) -> dict[str, Policy]:
    """Validate the config and return the policies to simulate, ``ideal`` last."""
    policies = {p.name: Policy(p.name, p.lock, p.stale) for p in cfg.policies}
    if len(policies) != len(cfg.policies) or "ideal" in policies:
        raise ConfigError(
            "policy names must be unique and must not be 'ideal' (it is always included)"
        )
    policies["ideal"] = IDEAL
    for pair in cfg.headroom:
        if pair.baseline not in policies or pair.candidate not in policies:
            raise ConfigError(f"headroom pair {pair} names an unknown policy")
    if cfg.plot_recheck_think_fraction not in cfg.recheck_think_fractions:
        raise ConfigError("plot_recheck_think_fraction must be one of recheck_think_fractions")
    if 1 in cfg.workers:
        raise ConfigError(
            "workers must not include 1: one worker is always simulated as the reference"
        )
    return policies


def load_collect_run(src: Path) -> CollectRun:
    with open(src / "tasks.json", encoding="utf-8") as fh:
        tasks = {t["id"]: t for t in json.load(fh)}
    results = {
        (r["task"], r["repetition"], r["mode"]): r for r in read_jsonl(src / "results.jsonl")
    }
    traces: dict[tuple[str, int], dict[str, EpisodeTrace]] = defaultdict(dict)
    whole_seconds: dict[tuple[str, int], float] = defaultdict(float)
    sources = set()
    for row in read_jsonl(src / "traces.jsonl"):
        t = EpisodeTrace.from_json(row["trace"])
        sources.add(t.timing_source)
        if row["mode"] == "decomposed":
            traces[(t.task_id, t.repetition)][t.job_id] = t
        else:  # monolithic: one whole-task episode (or, for the oracle, its jobs back to back)
            whole_seconds[(t.task_id, t.repetition)] += trace_seconds(t)
    return CollectRun(tasks, results, traces, whole_seconds, sources)


def replay(
    cfg: SimulateConfig, policies: dict[str, Policy], run: CollectRun, run_dir: Path
) -> Replay:
    """Simulate every usable run under every setting; write one row per simulation."""
    out = Replay()
    for (task_id, rep), by_job in sorted(run.traces.items()):
        spec = run.tasks[task_id]
        if set(by_job) != {j["id"] for j in spec["jobs"]}:
            out.skipped_incomplete += 1
            continue
        if cfg.only_successful_runs and not run.results[(task_id, rep, "decomposed")]["success"]:
            out.skipped_failed += 1
            continue
        out.used += 1
        jobs = tuple(to_sim_job(by_job[j["id"]], tuple(j["deps"])) for j in spec["jobs"])
        serial = simulate(jobs, 1, IDEAL, 0.0, cfg.job_start_overhead_s).makespan_s
        if (task_id, rep) in run.whole_seconds:
            out.decomposition_cost[task_id].append(serial / run.whole_seconds[(task_id, rep)])
        for k, v in interference(jobs).items():
            out.overlap_by_family[spec["family"]][k] += v
        for w in cfg.workers:
            bound = lower_bound(jobs, w, cfg.job_start_overhead_s)
            for frac in cfg.recheck_think_fractions:
                for name, pol in policies.items():
                    r = simulate(jobs, w, pol, frac, cfg.job_start_overhead_s)
                    if r.makespan_s < bound - 1e-9:
                        raise AssertionError(
                            f"{task_id}: makespan {r.makespan_s} below lower bound {bound}"
                        )
                    out.makespan[(task_id, rep, frac, name, w)] = r.makespan_s
                    out.speedup[(name, w, frac)][task_id].append(serial / r.makespan_s)
                    append_jsonl(
                        run_dir / "sim_rows.jsonl",
                        {
                            "task": task_id,
                            "family": spec["family"],
                            "repetition": rep,
                            "policy": name,
                            "workers": w,
                            "recheck_think_fraction": frac,
                            "serial_s": serial,
                            "makespan_s": r.makespan_s,
                            "speedup": serial / r.makespan_s,
                            "rechecks": r.rechecks,
                            "recheck_s": r.recheck_s,
                            "wait_s": r.wait_s,
                            "lower_bound_s": bound,
                        },
                    )
    return out


def summarise(
    cfg: SimulateConfig, rep: Replay, sources: set[str], rng: np.random.Generator
) -> dict[str, Any]:
    """Aggregate the replay.  The order of bootstrap calls fixes the random stream: keep it."""
    summary: dict[str, Any] = {
        "timing_sources": sorted(sources),
        "runs_used": rep.used,
        "runs_skipped_failed_checker": rep.skipped_failed,
        "runs_skipped_incomplete": rep.skipped_incomplete,
        "speedup_over_one_worker": [
            {
                "policy": name,
                "workers": w,
                "recheck_think_fraction": frac,
                **_gmean_ci(by_task, cfg.bootstrap, rng),
            }
            for (name, w, frac), by_task in sorted(rep.speedup.items())
        ],
        "headroom": [],
        "interference_by_family": {f: dict(v) for f, v in rep.overlap_by_family.items()},
    }
    if rep.decomposition_cost:
        summary["decomposed_serial_time_over_monolithic_time"] = _gmean_ci(
            rep.decomposition_cost, cfg.bootstrap, rng
        )
    for pair in cfg.headroom:
        for w in cfg.workers:
            for frac in cfg.recheck_think_fractions:
                ratio = _headroom_ratios(rep.makespan, pair, w, frac)
                summary["headroom"].append(
                    {
                        "baseline": pair.baseline,
                        "candidate": pair.candidate,
                        "workers": w,
                        "recheck_think_fraction": frac,
                        "baseline_time_over_candidate_time": _gmean_ci(ratio, cfg.bootstrap, rng),
                    }
                )
    return summary


def _headroom_ratios(
    makespan: dict[tuple[str, int, float, str, int], float],
    pair: HeadroomPair,
    workers: int,
    frac: float,
) -> dict[str, list[float]]:
    """Per task: baseline makespan / candidate makespan, one value per repetition."""
    ratio: dict[str, list[float]] = defaultdict(list)
    for (task_id, rep, f, name, w), m in makespan.items():
        if (f, name, w) == (frac, pair.baseline, workers):
            ratio[task_id].append(m / makespan[(task_id, rep, frac, pair.candidate, workers)])
    return ratio


def plot_speedup(
    cfg: SimulateConfig,
    policies: dict[str, Policy],
    summary: dict[str, Any],
    sources: set[str],
    run_dir: Path,
) -> None:
    frac = cfg.plot_recheck_think_fraction
    series = {}
    for name in policies:
        rows = [
            next(
                r
                for r in summary["speedup_over_one_worker"]
                if (r["policy"], r["workers"], r["recheck_think_fraction"]) == (name, w, frac)
            )
            for w in cfg.workers
        ]
        series[name] = (
            [r["estimate"] for r in rows],
            [r["low"] for r in rows],
            [r["high"] for r in rows],
        )
    note = " (SYNTHETIC timing: pipeline test, not a result)" if "synthetic" in sources else ""
    line_chart(
        run_dir / "speedup.png",
        f"Speedup over one worker, by coordination policy{note}",
        "workers",
        "speedup (geometric mean over tasks)",
        list(cfg.workers),
        series,
        reference=1.0,
    )


def main(config_path: str) -> str:
    cfg = load(SimulateConfig, config_path)
    policies = _policies(cfg)
    run = load_collect_run(Path(cfg.collect_run))
    if "synthetic" in run.timing_sources and not cfg.allow_synthetic_timing:
        raise RuntimeError(
            "these traces have synthetic timing (oracle runs); they test the pipeline and are not "
            "results. Set allow_synthetic_timing: true to analyse them anyway."
        )

    run_dir = create_run_dir(cfg.output_root, "exp1_simulate", cfg.tag)
    write_manifest(run_dir, "exp1_simulate", cfg, {"timing_sources": sorted(run.timing_sources)})
    rng = np.random.default_rng(cfg.bootstrap.seed)

    rep = replay(cfg, policies, run, run_dir)
    if rep.used == 0:
        raise RuntimeError(
            "no usable runs: every decomposed run was incomplete or failed the checker"
        )
    summary = summarise(cfg, rep, run.timing_sources, rng)
    write_json(run_dir / "summary.json", summary)
    plot_speedup(cfg, policies, summary, run.timing_sources, run_dir)
    print(
        f"runs used: {rep.used}; skipped (failed checker): {rep.skipped_failed}; "
        f"skipped (incomplete): {rep.skipped_incomplete}"
    )
    print(f"run directory: {run_dir}")
    return str(run_dir)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m pathboard.exp1.simulate CONFIG.yaml")
    main(sys.argv[1])
