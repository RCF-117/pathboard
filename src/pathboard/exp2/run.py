"""Experiment 2: observer precision.

For every action instance, rendering mode and observer, compare the paths the
observer reports with the paths that really changed.

Usage:  python -m pathboard.exp2.run CONFIG.yaml
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from ..browser.executor import ApiActor, SemanticExecutor
from ..browser.groundtruth import GroundTruthProbe
from ..browser.observer import Observer
from ..browser.session import BrowserConfig, BrowserSession
from ..config import ConfigError, load
from ..episodes import open_tab, validate_timing
from ..plots import grouped_bars
from ..runlog import append_jsonl, create_run_dir, write_json, write_manifest
from ..stats import cluster_bootstrap_ci
from ..testbed.server import RenderMode, ServerConfig, TestbedServer
from ..testbed.state import Ledger, LedgerConfig
from .catalog import CATALOG, ActionInstance, make_instance
from .scoring import score


@dataclass(frozen=True)
class ObserverSpec:
    name: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class BootstrapConfig:
    n_boot: int
    confidence: float
    seed: int


@dataclass(frozen=True)
class Exp2Config:
    tag: str
    output_root: str
    seed: int
    ledger: LedgerConfig
    render_modes: tuple[RenderMode, ...]
    observers: tuple[ObserverSpec, ...]
    action_types: tuple[str, ...]
    instances_per_type: int
    poll_interval_ms: int  # must be > 0: changes by another actor reach the page by polling
    api_latency_ms: int
    page_size: int
    browser: BrowserConfig
    bootstrap: BootstrapConfig


def run_instance(
    session: BrowserSession, cfg: Exp2Config, mode: RenderMode, inst: ActionInstance
) -> list[dict[str, Any]]:
    server_cfg = ServerConfig(
        "127.0.0.1", 0, mode, cfg.poll_interval_ms, cfg.api_latency_ms, cfg.page_size
    )
    with TestbedServer(cfg.ledger, server_cfg) as server:
        tab = open_tab(session, server, "agent", "ref")
        try:
            executor = SemanticExecutor(tab)
            for op, kwargs in inst.setup:
                executor.run(op, **kwargs)
            tab.gt_wait_applied(server.seq())
            probe = GroundTruthProbe(tab, server)
            observers = {spec.name: Observer(tab, spec.sources) for spec in cfg.observers}

            flat_before, view_before = server.flat(), tab.gt_view_leaves()
            shown_before = set(tab.gt_displayed())
            mark = probe.mark()
            for o in observers.values():
                o.begin()
            op, kwargs = inst.action
            if op == "remote":
                ApiActor(server.base_url, "other").call(
                    kwargs["method"], kwargs["path"], kwargs["body"]
                )
            else:
                executor.run(op, **kwargs)
            tab.gt_wait_applied(server.seq())
            claims = {name: o.end() for name, o in observers.items()}

            truth = frozenset(probe.data_writes_since(mark, None) | probe.view_writes_since(mark))
            view_after = tab.gt_view_leaves()
            universe = frozenset(
                set(flat_before) | set(server.flat()) | set(view_before) | set(view_after)
            )
            visible = frozenset(
                shown_before | set(tab.gt_displayed()) | set(view_before) | set(view_after)
            )
        finally:
            session.close_tab(tab)
    rows = []
    for name, claimed in claims.items():
        s = score(truth, claimed, universe, visible)
        rows.append(
            {
                "render_mode": mode,
                "observer": name,
                "action_type": inst.type,
                "effect": inst.effect,
                **asdict(s),
                "safe": s.safe,
                "truth": sorted(truth),
                "claimed": sorted(claimed),
            }
        )
    return rows


def _ci(
    by_type: dict[str, list[float]], boot: BootstrapConfig, rng: np.random.Generator
) -> dict[str, float]:
    est, low, high = cluster_bootstrap_ci(
        by_type, lambda x: float(np.mean(x)), boot.n_boot, boot.confidence, rng
    )
    return {"estimate": est, "low": low, "high": high}


def main(config_path: str) -> str:
    cfg = load(Exp2Config, config_path)
    unknown = [a for a in cfg.action_types if a not in CATALOG]
    if unknown:
        raise ConfigError(f"unknown action types {unknown}; known: {sorted(CATALOG)}")
    if cfg.poll_interval_ms <= 0:
        raise ConfigError("poll_interval_ms must be > 0 for this experiment")
    validate_timing(
        ServerConfig(
            "127.0.0.1",
            0,
            cfg.render_modes[0],
            cfg.poll_interval_ms,
            cfg.api_latency_ms,
            cfg.page_size,
        ),
        cfg.browser.settle_quiet_ms,
    )
    led = Ledger(cfg.ledger)
    instances = [
        make_instance(a, led, cfg.page_size, cfg.seed, r)
        for a in cfg.action_types
        for r in range(cfg.instances_per_type)
    ]
    run_dir = create_run_dir(cfg.output_root, "exp2", cfg.tag)
    write_manifest(run_dir, "exp2", cfg, {"n_instances": len(instances)})
    write_json(run_dir / "instances.json", instances)

    rows: list[dict[str, Any]] = []
    with BrowserSession(cfg.browser) as session:
        for mode in cfg.render_modes:
            for inst in instances:
                for row in run_instance(session, cfg, mode, inst):
                    append_jsonl(run_dir / "rows.jsonl", row)
                    rows.append(row)
            print(f"{mode}: {len(instances)} action instances done", flush=True)

    # Aggregate.  Instances of one action type are not independent, so the
    # bootstrap resamples action types.
    rng = np.random.default_rng(cfg.bootstrap.seed)
    metrics = ("safe", "recall", "visible_recall", "precision")
    cells: dict[tuple[str, str], dict[str, dict[str, list[float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    verdicts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    per_type: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        key = (r["render_mode"], r["observer"])
        for m in metrics:
            cells[key][m][r["action_type"]].append(float(r[m]))
        verdicts[key][r["verdict"]] += 1
        per_type[(r["render_mode"], r["observer"], r["action_type"])].append(r)
    summary = {
        "overall": [
            {
                "render_mode": mode,
                "observer": obs,
                "n_actions": sum(verdicts[(mode, obs)].values()),
                "verdicts": dict(verdicts[(mode, obs)]),
                **{m: _ci(cells[(mode, obs)][m], cfg.bootstrap, rng) for m in metrics},
            }
            for (mode, obs) in sorted(cells)
        ],
        "by_action_type": [
            {
                "render_mode": mode,
                "observer": obs,
                "action_type": a,
                "effect": rs[0]["effect"],
                "n": len(rs),
                **{m: float(np.mean([float(r[m]) for r in rs])) for m in metrics},
                "mean_leaves_changed": float(np.mean([r["n_truth"] for r in rs])),
                "mean_leaves_claimed": float(np.mean([r["n_claimed"] for r in rs])),
            }
            for (mode, obs, a), rs in sorted(per_type.items())
        ],
    }
    write_json(run_dir / "summary.json", summary)
    names = [o.name for o in cfg.observers]
    for metric, title, fname in (
        ("safe", "Share of actions where the observer missed nothing", "safe.png"),
        ("precision", "Precision: changed leaves / leaves the observer named", "precision.png"),
    ):
        series = {}
        for mode in cfg.render_modes:
            got = {r["observer"]: r[metric] for r in summary["overall"] if r["render_mode"] == mode}
            series[mode] = (
                [got[n]["estimate"] for n in names],
                [got[n]["low"] for n in names],
                [got[n]["high"] for n in names],
            )
        grouped_bars(run_dir / fname, title, "observer", metric, names, series, ymax=1.05)
    print(f"run directory: {run_dir}")
    return str(run_dir)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m pathboard.exp2.run CONFIG.yaml")
    main(sys.argv[1])
