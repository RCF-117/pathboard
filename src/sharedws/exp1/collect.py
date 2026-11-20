"""Experiment 1, step 1: record traces.

For every task and repetition this records

* ``monolithic``: one agent does the whole task (the single-agent baseline), and
* ``decomposed``: a fresh agent does each job, one after another, on the same
  data (the per-job traces that the simulator replays with several workers).

Usage:  python -m sharedws.exp1.collect CONFIG.yaml
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Literal

from ..browser.session import BrowserConfig, BrowserSession, HandleMode
from ..config import ConfigError, load
from ..episodes import (LLMAgentSettings, answer_of, judge, run_jobs_llm, run_jobs_oracle, run_whole_llm,
                        trace_seconds, validate_timing)
from ..llm.factory import LLMConfig, make_client
from ..runlog import append_jsonl, create_run_dir, write_json, write_manifest
from ..tasks.oracle import SyntheticTiming
from ..tasks.suite import SuiteConfig, build_suite
from ..testbed.server import ServerConfig, TestbedServer

Mode = Literal["monolithic", "decomposed"]


@dataclass(frozen=True)
class CollectConfig:
    tag: str
    output_root: str
    suite: SuiteConfig
    server: ServerConfig
    browser: BrowserConfig
    handle_mode: HandleMode
    repetitions: int
    modes: tuple[Mode, ...]
    agent_kind: Literal["llm", "oracle"]
    llm: LLMConfig | None  # required when agent_kind is "llm"
    llm_agent: LLMAgentSettings | None  # required when agent_kind is "llm"
    oracle: SyntheticTiming | None  # required when agent_kind is "oracle"


def _validate(cfg: CollectConfig) -> None:
    if cfg.repetitions < 1 or not cfg.modes:
        raise ConfigError("repetitions must be >= 1 and modes non-empty")
    if cfg.agent_kind == "llm" and (cfg.llm is None or cfg.llm_agent is None or cfg.oracle is not None):
        raise ConfigError("agent_kind 'llm' needs 'llm' and 'llm_agent', and 'oracle' must be null")
    if cfg.agent_kind == "oracle" and (cfg.oracle is None or cfg.llm is not None or cfg.llm_agent is not None):
        raise ConfigError("agent_kind 'oracle' needs 'oracle', and 'llm'/'llm_agent' must be null")
    validate_timing(cfg.server, cfg.browser.settle_quiet_ms)


def main(config_path: str) -> str:
    cfg = load(CollectConfig, config_path)
    _validate(cfg)
    tasks = build_suite(cfg.suite)
    llm = make_client(cfg.llm) if cfg.agent_kind == "llm" else None
    run_dir = create_run_dir(cfg.output_root, "exp1_collect", cfg.tag)
    write_manifest(run_dir, "exp1_collect", cfg, {"n_tasks": len(tasks), "agent": llm.name if llm else "oracle"})
    write_json(run_dir / "tasks.json", list(tasks))
    transcript = run_dir / "transcripts.jsonl" if llm else None

    with BrowserSession(cfg.browser) as session:
        for task in tasks:
            for rep in range(cfg.repetitions):
                for mode in cfg.modes:
                    with TestbedServer(task.ledger, cfg.server) as server:
                        initial = server.flat()
                        if llm is None:
                            traces = run_jobs_oracle(session, server, task, rep, cfg.oracle,
                                                     fresh_tab_per_job=(mode == "decomposed"))
                        elif mode == "monolithic":
                            traces = [run_whole_llm(session, server, task, rep, llm, cfg.llm_agent, cfg.handle_mode,
                                                    "none", None, transcript, None)]
                        else:
                            traces = run_jobs_llm(session, server, task, rep, llm, cfg.llm_agent, cfg.handle_mode,
                                                  transcript)
                        result = judge(task, initial, server, "agent", answer_of(task, traces))
                    for t in traces:
                        append_jsonl(run_dir / "traces.jsonl", {"mode": mode, "trace": t.to_json()})
                    row = {
                        "task": task.id, "family": task.family, "repetition": rep, "mode": mode,
                        "success": result.success, "unmet": result.unmet, "collateral": result.collateral,
                        "answer_ok": result.answer_ok, "all_finished": all(t.finished for t in traces),
                        "n_steps": sum(len(t.steps) for t in traces),
                        "agent_seconds": sum(trace_seconds(t) for t in traces),
                        "input_tokens": sum(s.input_tokens or 0 for t in traces for s in t.steps),
                        "output_tokens": sum(s.output_tokens or 0 for t in traces for s in t.steps),
                    }
                    append_jsonl(run_dir / "results.jsonl", row)
                    print(f"{task.id} rep={rep} {mode}: success={result.success} steps={row['n_steps']} "
                          f"time={row['agent_seconds']:.1f}s", flush=True)
    print(f"run directory: {run_dir}")
    return str(run_dir)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m sharedws.exp1.collect CONFIG.yaml")
    main(sys.argv[1])
