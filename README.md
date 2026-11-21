# pathboard: several AI agents working in one live app at once

**pathboard** is a research project on making computer-use agents faster. Agents
that operate software (click, type, save) act one step at a time. We let several
agents work in the same live web app at the same time, the way a person
multitasks, with a coordinator written in code that keeps them from breaking
each other's work.

## The problem

Today's multi-agent systems give each agent its own virtual machine, or let only
one agent touch the app while the others wait. Inside one live application, work
still runs one step at a time.

Letting agents share the app naively is unsafe. An edit is a *move* of several
steps: read the value, open the editor, think, type, save. That takes seconds.
If another agent saves in between, the first agent saves a value based on what
it read earlier, and the other agent's change is silently lost. In our tests
with four agents editing one price and no coordination, 5 of 6 runs ended with
a wrong final value.

## The idea: a per-move workboard

Every agent declares its next move to a **workboard**, a program that decides
each move before it runs, using what the app's pages show. A model is called
only when that is not enough.

- **Allow** the move if nothing in progress depends on its target.
- **Queue** it if another agent is in the middle of editing the same data.
- **Fork** it into its own copy (for example a new tab) if it would otherwise wait.
- **Recheck**: tell the agent to look again if what it read is out of date.

State is named by paths from coarse to fine (for example
`invoice/INV-1014/line/L2/unit_price`), so two agents editing different fields
of one invoice never block each other. The board holds a path for a whole move,
not one click, and keeps a version for every path so it can tell when what an
agent read has gone stale.

**Example.** Four agents each add an amount to the same price. Without the board
they read the same old price and overwrite each other. With the board, one agent
edits while the others wait for that one edit, then each reads the new price and
adds its amount. Agents working on different fields are never held up.

## What is built

- **A test app** ("Ledger", an invoice app) that runs locally, resets exactly
  from a seed and records the true effect of every action, so every final state
  can be checked.
- **Agents** that read the page as text and act through a real browser,
  supporting OpenAI and Anthropic models.
- **The board**, live: path holds per move, a queue, leases, and read marks with
  versions. It works without access to the app's server: versions come from
  each agent's own page, so it can sit on top of a third-party app.
- **Experiments** comparing the board with no coordination, a simple lock, and a
  save-time check, with significance tests. Results are in `first_results/`.

## Results so far

Four agents running at the same time in one app, each with its own job:

| Question | Result |
|---|---|
| Is coordination needed? | Yes. With none, 5 of 6 same-field runs lost an update. |
| Is it faster than one agent? | About 2.1x faster where jobs touch different fields. Where every job edits the same field the jobs must take turns, so it is no faster, as expected. |
| Board vs a simple per-action lock (realistic change detection) | Board correct in 12/12 runs, lock in 3/12 (p = 0.0002). |
| Board vs a save-time check (gpt-6.2-sol) | 12/12 vs 11/12 correct; the board was 1.14x faster and about 40% cheaper per run. |
| Board vs a save-time check (gpt-6-luna, a small model) | 10/12 vs 1/12 correct (p = 0.0006): refused saves confuse a small model, and the board avoids them. |
| Without access to the app's server | As safe as with it (18/18). Edits made by someone outside the board are caught once they reach the agent's page. |

Limits: one test app, tasks we wrote, 6-18 runs per comparison. Full tables,
logs and every agent's transcript are in `first_results/README.md`.

## What is next

1. **Finish the board's design.** After each save, the board confirms the write
   in code and the agent's job ends; when a check fails, the agent is told what
   changed and by whom. Our runs show this is needed: without it, a small model
   that sees the price change after its own save may add its amount twice.
2. **More of the design:** move classes (read, local edit, view-wide, final),
   older-job-wins, forking into a new tab, and a planner that splits a task into
   jobs.
3. **More tasks and a real web app**, so results do not depend on our test app.

## Model budget

A test of 4 agents takes about 35 model calls (about 8 per agent) and costs about
$0.01 on a small model or $0.10 on a strong one. Each experiment is designed to
run only the tests it needs. About 400 strong-model tests a month, plus reruns,
fit in $50. Every paid call goes through a hard monthly spending cap
(`llm/budget.py`), so runs stop before the budget is passed.

# Technical details

The experiments below were built first, to test the assumptions the idea rests on.

| # | Question | Needs a model? | Entry point |
|---|----------|----------------|-------------|
| 1 | **Headroom.** With real agent timings, how much faster is path-level coordination than one global lock, and than one agent? | yes (to record traces) | `pathboard.exp1.collect`, then `pathboard.exp1.simulate` |
| 2 | **Observer precision.** Can plain code tell which paths an action changed? | no | `pathboard.exp2.run` |
| 3 | **Worker robustness.** Does an agent stay correct when the page changes between looking and acting? | yes | `pathboard.exp3.run` |
| 4 | **Live concurrency.** With several real agents acting at once in one app, which coordination is safe, and which is fast? | yes | `pathboard.exp4.run` |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
pip freeze > requirements.lock        # record the exact versions you ran with
pytest                                # 153 tests, about 6 minutes; -m "not browser" for the fast ones
```

For experiments 1 and 3, set the key in your shell (it is never written to a file or log)
and put the exact model id in the config (`llm.settings.model`); there is no default.
Two providers are supported, each with its own configs:

| Provider | Key | Configs |
|----------|-----|---------|
| Anthropic (Claude) | `ANTHROPIC_API_KEY` | `exp1_collect.yaml`, `exp3.yaml` |
| OpenAI | `OPENAI_API_KEY` | `exp1_collect_openai.yaml`, `exp3_openai.yaml` |

```bash
export ANTHROPIC_API_KEY=...          # type this yourself
export OPENAI_API_KEY=...             # or this, for the OpenAI configs
export PATHBOARD_MONTHLY_BUDGET_USD=50  # required: hard monthly cap on model spend
```

Every paid model call goes through a spending cap (`llm/budget.py`). Before each call
it adds this calendar month's spend from a ledger shared by all runs on the machine
(`~/.pathboard/spend.jsonl`), the calls in flight, and the worst case of the new call.
If that passes the cap, the call is refused and the run stops. Costs are list prices
times reported tokens, so they estimate the bill; also set a budget limit in the
provider's dashboard, which is the only limit that covers calls made elsewhere.

The OpenAI configs differ from the Claude ones only in the `llm` block. Its
`reasoning_effort` changes how long the model thinks, which Experiment 1
measures, so keep it fixed within a comparison. `max_output_tokens` counts
reasoning tokens too; a reply cut off by it stops the run rather than being
recorded as an action. See the docstring of `llm/openai_client.py` for how the
request is built to match the Claude one (same prompt, same tool schema, one
forced tool call).

## Running

```bash
# Look at the testbed by hand
python -m pathboard.testbed.server configs/testbed.yaml

# Experiment 2 (no key needed)
python -m pathboard.exp2.run configs/exp2.yaml

# Experiment 1
python -m pathboard.exp1.collect configs/exp1_collect.yaml       # records traces with the model
#   (or configs/exp1_collect_openai.yaml for an OpenAI model)
#   put the printed run directory into configs/exp1_simulate.yaml (collect_run)
python -m pathboard.exp1.simulate configs/exp1_simulate.yaml

# Experiment 3
python -m pathboard.exp3.run configs/exp3.yaml                   # or configs/exp3_openai.yaml

# Experiment 4 (live: all jobs of a task run at once, one agent each)
python -m pathboard.exp4.run configs/exp4.yaml

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
The policy grid crosses both rules (including `global_lock+target`), so the
headroom pairs can separate what the lock rule buys from what the staleness
rule buys. Each policy also has a lock `scope`: `action` (a lock covers one
action) or `move` (it covers a whole state-changing move, from the step that
begins an edit to the step that writes, thinking included; moves are cut from
the recorded writes by `moves_of`).

Task families (`tasks/families.py`) differ in how jobs relate. Six have jobs
that never touch the same entity; `shared_invoice` is the contended one: all
jobs edit one invoice, so every write changes what the other jobs' pages show
(totals, line list) while no job's target is ever touched by another. It is
the family where target-level staleness should matter most.
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

**Experiment 4.** All jobs of a task run *at the same time*, one agent each, in
separate tabs of one running app, through one live board (`live/board.py`:
`none`, a `global` lock or `path` holds, per `action` or per `move`, with an
optional target check). Nothing is replayed, so a broken or lost edit really
happens and the final-state check sees it. The `increment` family is the
lost-update test: every job adds its own amount to the same field, so the
right final value is the original plus all amounts. `no_board` is the
positive control that should lose updates.

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
  llm/       base.py anthropic_client.py openai_client.py factory.py
  agent/     loop.py                           observe, decide, act, record
  exp1/ exp2/ exp3/
configs/  tests/
```

To add another model provider, write a client with the `decide` method from
`llm/base.py` and a branch in `llm/factory.py`; nothing else changes
(`llm/openai_client.py` is a worked example, with tests in `tests/test_openai_client.py`).
