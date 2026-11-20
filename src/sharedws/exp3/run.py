"""Experiment 3: worker robustness under interference.

One agent does a whole task while a scripted interferer changes the workspace.
Conditions vary what the interferer does, when it does it (inside the window
between the agent looking and acting, or just before the agent looks), whether
the agent is told to look again, and how element handles are resolved.

Usage:  python -m sharedws.exp3.run CONFIG.yaml
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..agent.loop import Mitigation
from ..browser.session import BrowserConfig, BrowserSession, HandleMode
from ..config import ConfigError, load
from ..episodes import LLMAgentSettings, answer_of, judge, run_whole_llm, trace_seconds, validate_timing
from ..llm.base import LLMClient
from ..llm.factory import LLMConfig, make_client
from ..plots import grouped_bars
from ..runlog import append_jsonl, create_run_dir, write_json, write_manifest
from ..stats import cluster_bootstrap_ci, wilson_interval
from ..tasks.model import Task
from ..tasks.suite import SuiteConfig, build_suite
from ..testbed.server import RenderMode, ServerConfig, TestbedServer
from .interferer import ACTOR, Interferer, Kind, Schedule, When


@dataclass(frozen=True)
class Condition:
    name: str
    interference: Kind
    when: When
    mitigation: Mitigation
    handle_mode: HandleMode


@dataclass(frozen=True)
class BootstrapConfig:
    n_boot: int
    confidence: float
    seed: int


@dataclass(frozen=True)
class Exp3Config:
    tag: str
    output_root: str
    suite: SuiteConfig
    render_mode: RenderMode
    poll_interval_ms: int  # must be > 0 so the interferer's changes reach the agent's page
    api_latency_ms: int
    page_size: int
    browser: BrowserConfig
    repetitions: int
    baseline: str  # name of the condition every other condition is compared with
    conditions: tuple[Condition, ...]
    schedule: Schedule
    interferer_seed: int
    llm: LLMConfig
    llm_agent: LLMAgentSettings
    bootstrap: BootstrapConfig


def run_one(session: BrowserSession, cfg: Exp3Config, task: Task, repetition: int, cond: Condition,
            llm: LLMClient, transcript) -> tuple[dict[str, Any], Any]:
    """One episode of one condition. Returns the result row and the trace."""
    server_cfg = ServerConfig("127.0.0.1", 0, cfg.render_mode, cfg.poll_interval_ms, cfg.api_latency_ms, cfg.page_size)
    with TestbedServer(task.ledger, server_cfg) as server:
        initial = server.flat()
        box: dict[str, Interferer] = {}

        def on_tab(tab) -> None:
            box["i"] = Interferer(cond.interference, cfg.schedule, task, server, tab,
                                  cfg.interferer_seed + repetition)

        def stale_window(step: int, _target: str | None) -> None:
            if cond.when == "stale_window" and box["i"].due(step):
                box["i"].fire(step)

        def before_observe(step: int) -> None:
            if cond.when == "before_observe" and box["i"].due(step):
                box["i"].fire(step)

        trace = run_whole_llm(session, server, task, repetition, llm, cfg.llm_agent, cond.handle_mode,
                              cond.mitigation, stale_window, transcript, on_tab, before_observe)
        result = judge(task, initial, server, "agent", answer_of(task, [trace]))
        agent_ops = [e for e in server.oplog_since(0) if e["actor"] != ACTOR]
    events = box["i"].events
    row = {
        "condition": cond.name, "task": task.id, "family": task.family, "repetition": repetition,
        "success": result.success, "unmet": result.unmet, "collateral": result.collateral,
        "answer_ok": result.answer_ok, "finished": trace.finished, "n_steps": len(trace.steps),
        "agent_seconds": trace_seconds(trace), "n_writes": len(agent_ops),
        "wrong_target_actions": sum(s.wrong_target for s in trace.steps),
        "stale_element_errors": sum(s.outcome == "stale_element" for s in trace.steps),
        "action_errors": sum(s.outcome == "action_error" for s in trace.steps),
        "rechecks": sum(s.outcome == "rechecked" for s in trace.steps),
        "events_fired": sum(e["applied"] for e in events), "events": events,
        "input_tokens": sum(s.input_tokens or 0 for s in trace.steps),
        "output_tokens": sum(s.output_tokens or 0 for s in trace.steps),
    }
    return row, trace


def summarise(rows: list[dict[str, Any]], baseline: str, boot: BootstrapConfig) -> dict[str, Any]:
    rng = np.random.default_rng(boot.seed)
    mean = lambda x: float(np.mean(x))  # noqa: E731
    by_cond: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_cond[r["condition"]].append(r)
    if baseline not in by_cond:
        raise ValueError(f"baseline condition {baseline!r} has no rows")
    base = {(r["task"], r["repetition"]): r for r in by_cond[baseline]}
    out = []
    for name, rs in by_cond.items():
        n, k = len(rs), sum(r["success"] for r in rs)
        p, lo, hi = wilson_interval(k, n, boot.confidence)
        entry: dict[str, Any] = {"condition": name, "episodes": n, "success": {"rate": p, "wilson_low": lo, "wilson_high": hi}}
        for metric in ("n_steps", "agent_seconds", "wrong_target_actions", "stale_element_errors", "rechecks",
                       "events_fired"):
            per_task: dict[str, list[float]] = defaultdict(list)
            for r in rs:
                per_task[r["task"]].append(float(r[metric]))
            est, low, high = cluster_bootstrap_ci(per_task, mean, boot.n_boot, boot.confidence, rng)
            entry[metric] = {"mean": est, "low": low, "high": high}
        entry["collateral_rate"] = mean([bool(r["collateral"]) for r in rs])
        if name != baseline:
            # Paired with the baseline on the same task and repetition; tasks are the clusters.
            diff: dict[str, list[float]] = defaultdict(list)
            for r in rs:
                diff[r["task"]].append(float(r["success"]) - float(base[(r["task"], r["repetition"])]["success"]))
            est, low, high = cluster_bootstrap_ci(diff, mean, boot.n_boot, boot.confidence, rng)
            entry["success_minus_baseline"] = {"estimate": est, "low": low, "high": high}
        by_family: dict[str, list[bool]] = defaultdict(list)
        for r in rs:
            by_family[r["family"]].append(r["success"])
        entry["success_by_family"] = {f: {"rate": mean(v), "n": len(v)} for f, v in sorted(by_family.items())}
        out.append(entry)
    return {"baseline": baseline, "conditions": out}


def main(config_path: str) -> str:
    cfg = load(Exp3Config, config_path)
    names = [c.name for c in cfg.conditions]
    if len(set(names)) != len(names) or cfg.baseline not in names:
        raise ConfigError("condition names must be unique and include the baseline")
    if cfg.poll_interval_ms <= 0:
        raise ConfigError("poll_interval_ms must be > 0 for this experiment")
    validate_timing(ServerConfig("127.0.0.1", 0, cfg.render_mode, cfg.poll_interval_ms, cfg.api_latency_ms,
                                 cfg.page_size), cfg.browser.settle_quiet_ms)
    tasks = build_suite(cfg.suite)
    llm = make_client(cfg.llm)
    run_dir = create_run_dir(cfg.output_root, "exp3", cfg.tag)
    write_manifest(run_dir, "exp3", cfg, {"n_tasks": len(tasks), "agent": llm.name})
    write_json(run_dir / "tasks.json", list(tasks))

    rows = []
    with BrowserSession(cfg.browser) as session:
        for task in tasks:
            for rep in range(cfg.repetitions):
                for cond in cfg.conditions:
                    row, trace = run_one(session, cfg, task, rep, cond, llm, run_dir / "transcripts.jsonl")
                    append_jsonl(run_dir / "rows.jsonl", row)
                    append_jsonl(run_dir / "traces.jsonl", {"condition": cond.name, "trace": trace.to_json()})
                    rows.append(row)
                    print(f"{task.id} rep={rep} {cond.name}: success={row['success']} steps={row['n_steps']} "
                          f"wrong_target={row['wrong_target_actions']} events={row['events_fired']}", flush=True)
    summary = summarise(rows, cfg.baseline, cfg.bootstrap)
    write_json(run_dir / "summary.json", summary)
    conds = summary["conditions"]
    grouped_bars(run_dir / "success.png", "Task success by condition", "condition", "success rate",
                 [c["condition"] for c in conds],
                 {"success": ([c["success"]["rate"] for c in conds], [c["success"]["wilson_low"] for c in conds],
                              [c["success"]["wilson_high"] for c in conds])}, ymax=1.05)
    print(f"run directory: {run_dir}")
    return str(run_dir)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m sharedws.exp3.run CONFIG.yaml")
    main(sys.argv[1])
