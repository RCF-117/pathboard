"""Experiment 4: live concurrent agents in one workspace.

Every job of a task gets its own agent, and all of them run *at the same time* against one
running app, each in its own tab (the browser's cheap fork), all going through one live board
(``live/board.py``).  Unlike Experiment 1 nothing is replayed: if one agent's change breaks
another agent's half-finished edit, it really happens and the final-state check sees it.

Per condition the board policy is (lock, scope) and the agent's notice rule is ``none`` or
``recheck_target`` (look again if the element about to be used changed; ground truth decides,
so this measures the policy with a perfect observer, not a detector).

Writes per run: rows.jsonl (one row per task x repetition x condition), traces.jsonl (one
trace per worker), transcripts/ (one file per worker), board.jsonl (per-worker board stats),
summary.json.

Usage:  python -m pathboard.exp4.run CONFIG.yaml
        python -m pathboard.exp4.run --summarise RUN_DIR
"""

from __future__ import annotations

import contextlib
import json
import random
import re
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np

from ..agent.checks import MITIGATIONS, REGISTRY_MODES
from ..agent.loop import AgentConfig, Mitigation, run_episode
from ..browser.session import BrowserConfig, BrowserSession, HandleMode
from ..config import ConfigError, from_dict, load
from ..episodes import WHOLE, LLMAgentSettings, open_tab, trace_seconds, validate_timing
from ..live.board import Board, Lock, Scope
from ..live.registry import Registry
from ..llm.base import LLMClient
from ..llm.factory import LLMConfig, make_client
from ..runlog import append_jsonl, create_run_dir, read_jsonl, write_json, write_manifest
from ..stats import cluster_bootstrap_ci, geometric_mean, wilson_interval
from ..tasks.model import Task, check
from ..tasks.suite import SuiteConfig, build_suite
from ..testbed.server import RenderMode, ServerConfig, TestbedServer
from ..trace import EpisodeTrace


Team = Literal["per_job", "one"]


@dataclass(frozen=True)
class Condition:
    name: str
    team: Team  # "per_job": one agent per job, all at once; "one": one agent does the whole task
    lock: Lock
    scope: Scope
    notice: Mitigation  # the check before acting; see agent/checks.py (recheck_any unused)


@dataclass(frozen=True)
class BootstrapConfig:
    n_boot: int
    confidence: float
    seed: int


@dataclass(frozen=True)
class OutsideConfig:
    """Someone outside the board (a person, another program) adding to the task's price.

    Every ``interval_s`` from ``first_s`` after the start, until ``edits`` are made or every
    worker has finished, they add ``amount_cents`` to the price the task's jobs edit, in one
    atomic step on the server.  The right final price then includes their additions too.
    """

    edits: int
    amount_cents: int
    first_s: float
    interval_s: float


@dataclass(frozen=True)
class Exp4Config:
    tag: str
    output_root: str
    suite: SuiteConfig
    render_mode: RenderMode
    poll_interval_ms: int  # > 0: other agents' changes must reach each page
    api_latency_ms: int
    page_size: int
    browser: BrowserConfig
    handle_mode: HandleMode
    repetitions: int
    order_seed: int  # conditions run in a shuffled order per task and repetition
    baseline: str  # condition every other one is compared with
    conditions: tuple[Condition, ...]
    lease_s: float  # longest a worker may keep a move's lock or holds
    start_timeout_s: float  # all workers must have their tab open within this time
    llm: LLMConfig
    llm_agent: LLMAgentSettings
    bootstrap: BootstrapConfig
    outside: OutsideConfig | None  # null: nobody outside the board edits during a run
    page_only: bool  # true: the board and check see only the agent's page (live/pageview.py)


def _check_page_only(cfg: Exp4Config) -> None:
    if not cfg.page_only:
        return
    for c in cfg.conditions:
        if c.notice not in ("none", *REGISTRY_MODES):
            raise ConfigError(
                f"condition {c.name}: page_only needs a check that does not read the server"
                f" (none or a registry mode), not {c.notice!r}"
            )


def _check_config(cfg: Exp4Config, tasks: tuple[Task, ...]) -> None:
    names = [c.name for c in cfg.conditions]
    if len(set(names)) != len(names) or cfg.baseline not in names:
        raise ConfigError("condition names must be unique and include the baseline")
    for c in cfg.conditions:
        if c.team not in ("per_job", "one"):
            raise ConfigError(f"condition {c.name}: team must be per_job or one")
        if c.team == "one" and (c.lock != "none" or c.notice != "none"):
            raise ConfigError(
                f"condition {c.name}: a single agent needs no board (lock and notice none)"
            )
        if c.notice not in MITIGATIONS or c.notice == "recheck_any":
            raise ConfigError(f"condition {c.name}: notice {c.notice!r} is not used here")
    if cfg.poll_interval_ms <= 0:
        raise ConfigError("poll_interval_ms must be > 0 so agents see each other's changes")
    _check_page_only(cfg)
    if cfg.outside is not None:
        for t in tasks:
            if t.family != "increment":
                raise ConfigError(f"{t.id}: outside edits are defined for increment tasks only")
    for t in tasks:
        if any(j.deps for j in t.jobs):
            raise ConfigError(f"{t.id}: jobs with dependencies cannot all start at once")


_PRICE = re.compile(r"^data/invoice/([^/]+)/line/([^/]+)/(unit_price_cents)$")


def _price_of(task: Task) -> re.Match[str]:
    (e,) = task.expectations
    m = _PRICE.match(e.path)
    if m is None:
        raise ConfigError(f"{task.id}: outside edits need a single line-price expectation")
    return m


def _outside_edits(
    oc: OutsideConfig,
    server: TestbedServer,
    task: Task,
    start: float,
    threads: list[threading.Thread],
) -> int:
    """Make the outside edits while the workers run; returns the total added, in cents."""
    iid, line, field = _price_of(task).groups()
    added = 0
    for k in range(oc.edits):
        due = start + oc.first_s + k * oc.interval_s
        while time.monotonic() < due and any(t.is_alive() for t in threads):
            time.sleep(0.05)
        if not any(t.is_alive() for t in threads):
            break
        server.add_to_line("outside", iid, line, field, oc.amount_cents)
        added += oc.amount_cents
    return added


def _without_outside(task: Task, final: dict[str, Any], added: int) -> dict[str, Any]:
    """The final state with the outside additions taken back out, to judge the agents' work."""
    if not added:
        return final
    path = _price_of(task).group(0)
    return {**final, path: final[path] - added}


def run_one(
    cfg: Exp4Config,
    task: Task,
    repetition: int,
    cond: Condition,
    llm_factory: Callable[[str], LLMClient],  # argument: the agent's stream name
    transcript_dir: Path | None,
) -> tuple[dict[str, Any], list[EpisodeTrace], dict[str, Any]]:
    """All jobs of ``task`` at once under ``cond``. Returns the row, the traces, board stats."""
    server_cfg = ServerConfig(
        "127.0.0.1", 0, cfg.render_mode, cfg.poll_interval_ms, cfg.api_latency_ms, cfg.page_size
    )
    board = Board(cond.lock, cond.scope, cfg.lease_s)
    # Registry modes: one registry per run, kept by the workers' own publishes.
    registry = Registry() if cond.notice in REGISTRY_MODES else None
    # The single-agent baseline is one worker with the whole task's goal and step limit.
    if cond.team == "one":
        work = [(WHOLE, task.goal, cfg.llm_agent.max_steps_whole)]
    else:
        work = [(j.id, j.goal, cfg.llm_agent.max_steps_job) for j in task.jobs]
    n = len(work)
    barrier = threading.Barrier(n + 1, timeout=cfg.start_timeout_s)
    traces: dict[int, EpisodeTrace] = {}
    ends: dict[int, float] = {}
    errors: dict[int, str] = {}

    with TestbedServer(task.ledger, server_cfg) as server:
        initial = server.flat()

        def worker(i: int) -> None:
            job_id, goal, max_steps = work[i]
            name = f"w{i + 1}"
            try:
                with BrowserSession(cfg.browser) as session:
                    tab = open_tab(session, server, name, cfg.handle_mode)
                    gate = board.gate(name)
                    llm = llm_factory(f"{task.id}/{repetition}/{cond.name}/{name}")
                    barrier.wait()
                    try:
                        traces[i] = run_episode(
                            tab,
                            server,
                            llm,
                            goal,
                            task.id,
                            job_id,
                            repetition,
                            AgentConfig(max_steps, cfg.llm_agent.history_actions),
                            cond.notice,
                            None,
                            None
                            if transcript_dir is None
                            else transcript_dir
                            / f"{task.id}_r{repetition}_{cond.name}_{name}.jsonl",
                            None,
                            gate,
                            registry,
                            cfg.page_only,
                        )
                    finally:
                        gate.finish()
                        ends[i] = time.monotonic()
            except Exception as exc:  # recorded in the row; the run goes on
                errors[i] = f"{type(exc).__name__}: {exc}"
                barrier.abort()

        threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n)]
        for t in threads:
            t.start()
        with contextlib.suppress(threading.BrokenBarrierError):  # a worker failed to start
            barrier.wait()
        start = time.monotonic()
        outside_added = 0
        if cfg.outside is not None:
            outside_added = _outside_edits(cfg.outside, server, task, start, threads)
        for t in threads:
            t.join()
        final = server.flat()
        writers = sorted({e["actor"] for e in server.oplog_since(0)})

    result = check(task, initial, _without_outside(task, final, outside_added), frozenset(), None)
    steps = [s for t in traces.values() for s in t.steps]
    stats = {w: asdict(s) for w, s in board.stats().items()}
    row = {
        "condition": cond.name,
        "task": task.id,
        "family": task.family,
        "repetition": repetition,
        "workers": n,
        "success": result.success and not errors,
        "unmet": result.unmet,
        "collateral": result.collateral,
        "errors": errors,
        "all_finished": len(traces) == n and all(t.finished for t in traces.values()),
        "wall_s": (max(ends.values()) - start) if ends and not errors else None,
        "agent_seconds": sum(trace_seconds(t) for t in traces.values()),
        "n_steps": len(steps),
        "wrong_target_actions": sum(s.wrong_target for s in steps),
        "stale_element_errors": sum(s.outcome == "stale_element" for s in steps),
        "action_errors": sum(s.outcome == "action_error" for s in steps),
        "rechecks": sum(s.outcome == "rechecked" for s in steps),
        "lock_wait_s": sum(s["wait_s"] for s in stats.values()),
        "lock_waits": sum(s["waits"] for s in stats.values()),
        "held_moves": sum(s["moves"] for s in stats.values()),
        "escalations": sum(s["escalations"] for s in stats.values()),
        "lease_expiries": sum(s["lease_expiries"] for s in stats.values()),
        "registry_publishes": None if registry is None else registry.publishes,
        "outside_added_cents": outside_added,
        "writers": writers,
        "input_tokens": sum(s.input_tokens or 0 for s in steps),
        "output_tokens": sum(s.output_tokens or 0 for s in steps),
    }
    return row, [traces[i] for i in sorted(traces)], stats


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
        k = sum(r["success"] for r in rs)
        p, lo, hi = wilson_interval(k, len(rs), boot.confidence)
        entry: dict[str, Any] = {
            "condition": name,
            "episodes": len(rs),
            "success": {"rate": p, "wilson_low": lo, "wilson_high": hi},
            "collateral_rate": mean([bool(r["collateral"]) for r in rs]),
            "error_rate": mean([bool(r["errors"]) for r in rs]),
        }
        for metric in (
            "wall_s",
            "n_steps",
            "wrong_target_actions",
            "stale_element_errors",
            "rechecks",
            "lock_wait_s",
            "lease_expiries",
            "escalations",
        ):
            per_task: dict[str, list[float]] = defaultdict(list)
            for r in rs:
                if r[metric] is not None:
                    per_task[r["task"]].append(float(r[metric]))
            if per_task:
                est, low, high = cluster_bootstrap_ci(
                    per_task, mean, boot.n_boot, boot.confidence, rng
                )
                entry[metric] = {"mean": est, "low": low, "high": high}
        if name != baseline:
            entry.update(_paired(rs, base, boot, rng))
        out.append(entry)
    return {"baseline": baseline, "conditions": out}


def _paired(
    rs: list[dict[str, Any]],
    base: dict[tuple[str, int], dict[str, Any]],
    boot: BootstrapConfig,
    rng: np.random.Generator,
) -> dict[str, Any]:
    """Success difference and wall-time ratio against the baseline on the same task and
    repetition; tasks are the bootstrap clusters."""
    out: dict[str, Any] = {}
    ratio: dict[str, list[float]] = defaultdict(list)
    diff: dict[str, list[float]] = defaultdict(list)
    for r in rs:
        b = base.get((r["task"], r["repetition"]))
        if b is None:
            continue
        diff[r["task"]].append(float(r["success"]) - float(b["success"]))
        if r["wall_s"] and b["wall_s"]:
            ratio[r["task"]].append(b["wall_s"] / r["wall_s"])
    mean = lambda x: float(np.mean(x))  # noqa: E731
    if diff:
        est, low, high = cluster_bootstrap_ci(diff, mean, boot.n_boot, boot.confidence, rng)
        out["success_minus_baseline"] = {"estimate": est, "low": low, "high": high}
    if ratio:
        est, low, high = cluster_bootstrap_ci(
            ratio, geometric_mean, boot.n_boot, boot.confidence, rng
        )
        out["baseline_time_over_this_time"] = {"estimate": est, "low": low, "high": high}
    return out


def main(config_path: str) -> str:
    cfg = load(Exp4Config, config_path)
    tasks = build_suite(cfg.suite)
    _check_config(cfg, tasks)
    validate_timing(
        ServerConfig(
            "127.0.0.1", 0, cfg.render_mode, cfg.poll_interval_ms, cfg.api_latency_ms, cfg.page_size
        ),
        cfg.browser.settle_quiet_ms,
    )
    probe = make_client(cfg.llm)  # fails early if the key or model id is missing
    run_dir = create_run_dir(cfg.output_root, "exp4", cfg.tag)
    write_manifest(run_dir, "exp4", cfg, {"n_tasks": len(tasks), "agent": probe.name})
    write_json(run_dir / "tasks.json", list(tasks))
    (run_dir / "transcripts").mkdir()
    order_rng = random.Random(cfg.order_seed)

    rows = []
    for task in tasks:
        for rep in range(cfg.repetitions):
            conds = list(cfg.conditions)
            order_rng.shuffle(conds)
            for cond in conds:
                row, traces, stats = run_one(
                    cfg,
                    task,
                    rep,
                    cond,
                    lambda stream: make_client(cfg.llm, stream),
                    run_dir / "transcripts",
                )
                append_jsonl(run_dir / "rows.jsonl", row)
                for t in traces:
                    append_jsonl(
                        run_dir / "traces.jsonl", {"condition": cond.name, "trace": t.to_json()}
                    )
                append_jsonl(
                    run_dir / "board.jsonl",
                    {"task": task.id, "repetition": rep, "condition": cond.name, "stats": stats},
                )
                rows.append(row)
                wall = f"{row['wall_s']:.1f}s" if row["wall_s"] else "n/a"
                print(
                    f"{task.id} rep={rep} {cond.name}: success={row['success']} wall={wall} "
                    f"steps={row['n_steps']} wrong_target={row['wrong_target_actions']} "
                    f"stale={row['stale_element_errors']} rechecks={row['rechecks']} "
                    f"wait={row['lock_wait_s']:.1f}s errors={len(row['errors'])}",
                    flush=True,
                )
    write_json(run_dir / "summary.json", summarise(rows, cfg.baseline, cfg.bootstrap))
    print(f"run directory: {run_dir}")
    return str(run_dir)


def resummarise(source_run: str) -> str:
    """Summarise a finished run again from its rows, into a new run directory."""
    src = Path(source_run)
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    raw = manifest["config"]
    for c in raw["conditions"]:  # runs made before the single-agent baseline existed
        c.setdefault("team", "per_job")
    cfg = from_dict(Exp4Config, raw, "manifest.config")
    run_dir = create_run_dir(cfg.output_root, "exp4_resummarise", cfg.tag)
    write_manifest(run_dir, "exp4_resummarise", cfg, {"source_run": str(src)})
    rows = read_jsonl(src / "rows.jsonl")
    write_json(run_dir / "summary.json", summarise(rows, cfg.baseline, cfg.bootstrap))
    print(f"run directory: {run_dir}")
    return str(run_dir)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--summarise":
        resummarise(sys.argv[2])
    elif len(sys.argv) == 2:
        main(sys.argv[1])
    else:
        raise SystemExit(
            "usage: python -m pathboard.exp4.run CONFIG.yaml\n"
            "       python -m pathboard.exp4.run --summarise RUN_DIR"
        )
