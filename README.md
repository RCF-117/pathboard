# pathboard: three pre-experiments for a shared-workspace workboard

Before building a per-move workboard for several computer-use agents in one live
workspace, these experiments test the three assumptions the idea rests on.

| # | Question | Needs a model? | Entry point |
|---|----------|----------------|-------------|
| 1 | **Headroom.** With real agent timings, how much faster is path-level coordination than one global lock, and than one agent? | yes (to record traces) | `pathboard.exp1.collect`, then `pathboard.exp1.simulate` |
| 2 | **Observer precision.** Can plain code tell which paths an action changed? | no | `pathboard.exp2.run` |
| 3 | **Worker robustness.** Does an agent stay correct when the page changes between looking and acting? | yes | `pathboard.exp3.run` |

Each one can stop the project early: if (1) shows under ~10% headroom over a global
lock, if (2) shows code cannot name changes safely, or if (3) shows agents break
under interference that cheap notification does not fix.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
pip freeze > requirements.lock        # record the exact versions you ran with
pytest                                # 76 tests, about 3 minutes; -m "not browser" for the fast ones
```

For experiments 1 and 3, set the key in your shell (it is never written to a file or log)
and put the exact model id in the config (`llm.settings.model`); there is no default.

```bash
export ANTHROPIC_API_KEY=...          # type this yourself
```

## Running

```bash
# Look at the testbed by hand
python -m pathboard.testbed.server configs/testbed.yaml

# Experiment 2 (no key needed)
python -m pathboard.exp2.run configs/exp2.yaml

# Experiment 1
python -m pathboard.exp1.collect configs/exp1_collect.yaml       # records traces with the model
#   put the printed run directory into configs/exp1_simulate.yaml (collect_run)
python -m pathboard.exp1.simulate configs/exp1_simulate.yaml

# Experiment 3
python -m pathboard.exp3.run configs/exp3.yaml

# Pipeline check without a model (scripted oracle, made-up think times; NOT results)
python -m pathboard.exp1.collect configs/exp1_collect_oracle.yaml
```

Every run writes a new directory `runs/<experiment>/<utc time>-<tag>/` with
`manifest.json` (full config, git commit, package versions), raw rows as JSON
lines, `summary.json` and figures. Start small first: set `tasks_per_family: 1`
and `repetitions: 1` to see cost and time per episode before a full run.

## The testbed

`pathboard/testbed` is "Ledger", a small invoice app (invoice list with sort,
filter, paging, inline edits and bulk edits; invoice pages with a draft form and
line items; vendors; payment batches, which are final; a summary page). It is
deterministic from a seed and logs the exact leaf paths every operation changed.

State is named by paths, coarse to fine, for example
`data/invoice/INV-1042/category` or `view/<tab>/invoices/sort`. Two paths
conflict exactly when one is a prefix of the other (`pathboard/paths.py`).

`render_mode` is an experimental variable: `granular` patches only what
changed, `rerender_keyed` redraws the whole view but keeps `data-*`
attributes, `rerender_anon` redraws with no attributes at all.

Ground truth lives in `window.__gt` and `/__gt/`. Agents and observers never
read it; only the harness does (methods named `gt_*`).

## What each experiment measures

**Experiment 1.** `collect` records, per task, one agent doing the whole task
(the single-agent baseline) and a fresh agent per job (authored job splits, so
planner quality is fixed). `simulate` replays the per-job traces with 2-4
workers under policies made of a lock rule (`global` or `path`) and a
staleness rule (`counter`, `view` = whole page, `target` = the element used).
`summary.json` gives speedup over one worker (geometric mean over tasks,
bootstrap over tasks), headroom ratios such as global lock time / path board
time, how often unordered job pairs really overlap, and decomposed-serial time
over monolithic time.

Read it with these limits in mind (they are in the simulator docstring too):
the replay assumes the agent repeats its steps after looking again and that a
step's writes are known in advance, so the path-board numbers are an upper
bound; workers have separate views (the design's "fork"); several agents in one
single view cannot be replayed from single-agent traces.

**Experiment 2.** Scripted actions of 27 types are run under each render mode
while observers watch. An observer's answer is scored against the real change.
`safe` means nothing was missed (what a coordinator needs first); `precision`
is how much of what it named really changed. Observers: `mutation_anchor`
(generic, follows DOM mutations), `model_diff` (hand-written page adapter),
`network` (rebuilds records from API replies), and combinations.

**Experiment 3.** One agent does a task while a scripted interferer changes the
workspace at fixed agent steps, either inside the look-then-act window or just
before the agent looks (control). Conditions also vary whether the agent is
told to look again (`recheck_any`, `recheck_target`; the notice uses ground
truth, so this measures the agent, not a detector) and how element handles
resolve (`ref` fails loudly when the element was replaced, `positional`
silently hits whatever is there now). Reported: success, steps, time,
wrong-target actions, stale-element errors, collateral changes, each against
the no-interference baseline on the same tasks.

## Checks built in

- Configs are strict: a missing or unknown key is an error; no experimental parameter has a default in code.
- Simulator: one worker equals serial time under every policy; hand-computed two-job cases; strict chains gain nothing; random instances never beat the lower bound (also asserted on every real run).
- Ledger: totals exact in integer cents; rejected operations change nothing; the server log equals the state diff.
- Tasks: every family is solved through the real page in all three render modes by an oracle; the checker rejects doing nothing, wrong answers and collateral changes.
- The network observer's record model is written independently of the server and must equal the server state.
- Oracle traces carry `timing_source: "synthetic"`, and the analysis refuses them unless `allow_synthetic_timing: true`.

## Layout

```
src/pathboard/
  paths.py config.py runlog.py stats.py trace.py plots.py episodes.py
  testbed/   state.py server.py static/        the Ledger app
  browser/   session.py page_scripts.py        tabs, text view, actions, handles
             executor.py groundtruth.py        scripted actions, ground-truth probe
             observer.py                       the observers under test
  tasks/     model.py families.py suite.py oracle.py
  llm/       base.py anthropic_client.py factory.py
  agent/     loop.py                           observe, decide, act, record
  exp1/ exp2/ exp3/
configs/  tests/
```

To use another model provider, add a client with the `decide` method from
`llm/base.py` and a branch in `llm/factory.py`; nothing else changes.
