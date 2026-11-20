# First results

Small runs, chosen to fit a ~$7 API budget (Experiment 4c is free: scripted agents). They show direction, not
statistically settled effects. Every folder holds the run's own `manifest.json`
(config, git commit, package versions).

| Folder | What | Model | Size |
|--------|------|-------|------|
| `exp2-sandbox-run/` | Experiment 2, observer precision | none needed | 27 action types x 5 instances x 3 render modes |
| `exp1-openai-budget/` | Experiment 1, headroom (`collect/` traces, `simulate/` analysis) | gpt-6.2-sol, reasoning medium | 6 families x 2 tasks x 1 repetition, both modes: 24 episodes, all successful |
| `exp1-openai-shared/` | Experiment 1 on the contended family `shared_invoice` | gpt-6.2-sol, reasoning medium | 3 tasks x 1 repetition, both modes: 6 episodes, all successful |
| `exp4-openai-budget/` | Experiment 4, live: all jobs of a task at once, one agent each | gpt-6.2-sol, reasoning medium | 6 tasks x 3 conditions x 2 repetitions: 36 runs of 4 agents (plus a 3-run smoke test) |
| `exp4b-openai-observed/` | Experiment 4b: as 4, look-again check by the Experiment 2 observer | gpt-6.2-sol | 6 increment tasks x 2 conditions x 2 repetitions: 24 runs of 4 agents |
| `exp4c-scripted-checks/` | Experiment 4c: board vs cheaper fixes, six check designs, scripted agents with recorded gpt-6.2-sol think times | none ($0) | 6 increment tasks x 6 conditions x 3 repetitions: 108 runs of 4 agents |
| `exp4d-openai-version/` | Experiment 4d: the board vs the save-time check, both with version read marks, real models | gpt-6.2-sol and gpt-6-luna | 6 increment tasks x 2 conditions x 2 repetitions per model, plus save-check re-runs and 3 smoke tests |
| `exp4-openai-single/` | Experiment 4 single-agent baseline: one agent does each whole task | gpt-6.2-sol | 12 tasks x 2 repetitions |
| `exp3-openai-budget/` | Experiment 3, worker robustness (`run/` episodes, `summary/` re-summary) | gpt-6.2-sol, reasoning medium | 4 families x 1 task x 3 conditions: 12 episodes, all successful |

Total API spend for the OpenAI runs, including a stopped first attempt and two
one-episode smoke tests: at most $6.11 for Experiments 1 and 3, and at most
$8.27 for Experiments 4, 4b and the single-agent baseline (a later $9.76 credit), and
about $5.09 for Experiment 4d (token counts x list price; a later $5 top-up), and
$0.17 for Experiment 4g (gpt-6-luna). Experiments 4c, 4e and 4f and the scripted part of 4g
are free (scripted agents).

## Experiment 1 (headroom)

Measured per step: think 1.7 s, act 0.2 s, observe 0.01 s.

Speedup over one worker doing the same jobs in turn (geometric mean over 12
tasks, 95% bootstrap interval; look-again cost 0.5 x think time):

| Policy | 2 workers | 4 workers |
|--------|-----------|-----------|
| global lock, look again after any write | 1.51 [1.31, 1.70] | 2.14 [1.65, 2.68] |
| global lock, look again if my page changed | 1.58 [1.35, 1.83] | 2.45 [1.82, 3.19] |
| path board, look again if my page changed | 1.59 [1.36, 1.84] | 2.54 [1.89, 3.36] |
| path board, look again if my target changed | 1.60 [1.36, 1.84] | 2.59 [1.89, 3.43] |
| ideal (no interaction, lower bound) | 1.60 [1.36, 1.84] | 2.59 [1.90, 3.44] |

Headroom, global-lock time / path-board time:

- against a page-aware global lock: 1.003 (2 workers) to 1.056 (4 workers),
  all upper interval ends below 1.10 (look-again cost 0.25-1.0 x think time).
  **Below the ~10% stop line.**
- against a global lock that looks again after any write: 1.03-1.32,
  growing with workers and with look-again cost.

Decomposed jobs done in turn take 1.45 x [1.31, 1.57] the time of one agent
doing the whole task, so against a single whole-task agent the net speedup is
about 1.1x with 2 workers and 1.75x with 4.

Why the lock rarely matters *when it covers one action*: an action holds the lock
for 0.2 s out of a ~1.9 s step. This changes when a lock covers a whole
state-changing move; see "Lock scope" below.

**Limit that matters most:** the authored job splits never have two jobs
writing the same stored leaf (write-write overlap 0 in every family), and two
families are fully ordered (`chain`) or fan-in (`approve_pay`). The path board
was not tested on contended work. A contended task family, `shared_invoice`,
has since been added (all jobs on one invoice); see the next section.

Re-analysing this run with `global_lock+target` added splits the small gains
of per-action locks: the lock rule (global vs path, both with target staleness)
gives 1.003-1.043; the staleness rule (page vs target, both under one lock)
gives 1.00-1.03.

## Experiment 1 on contended work (`shared_invoice`)

All jobs of a task edit one invoice; every write changes what the other jobs'
pages show. 3 tasks, measured think 2.7 s and act 0.17 s per step. The
re-analysis folder `exp1-openai-budget/simulate-with-isolating-policies/`
gives the same split for the six low-contention families.

Headroom (baseline time / candidate time), 95% bootstrap interval over tasks:

| Comparison | what differs | look-again cost | 2 workers | 4 workers |
|------------|--------------|-----------------|-----------|-----------|
| global+page / global+target | staleness rule only | 0.5 x think | 1.040 [1.031, 1.054] | 1.126 [1.057, 1.163] |
| global+page / global+target | staleness rule only | 1.0 x think | 1.095 [1.066, 1.118] | 1.291 [1.228, 1.325] |
| global+target / path+target | lock rule only | 0.5 or 1.0 | 1.006 [1.003, 1.013] | 1.013 [1.005, 1.019] |
| global+page / path+target | both | 0.5 x think | 1.046 [1.034, 1.067] | 1.141 [1.062, 1.185] |

`path_board+target` equals `ideal` (no interaction) on every task.
Decomposed-serial / monolithic time: 1.61 [1.57, 1.69] (one agent visits the
invoice once; four fresh agents each find it), so against a single whole-task
agent the net speedup is about 1.14x with 2 workers and 2.2x with 4.

Reading, for locks that cover one action: on contended work, knowing *which
element* a change touched (target staleness) is worth 4-29%, crossing the 10%
line at 4 workers; finer-grained locking adds at most 1.3% here and at most
4.3% in any family tested. With locks that cover a whole move the lock rule
matters far more; see the next section.

Limits: 3 tasks of one family; the simulator's path rules are an upper bound
(it knows each step's writes in advance); target staleness assumes something
can tell which paths an element stands for (Experiment 2's page reader plus
network observer named every real change in 96% of actions). Some clicks
(opening the invoice, Save) stand for the whole invoice, so their targets
overlap every other job's writes; in the replay they never coincided with a
foreign write, but with more workers or slower actions they would.

## Lock scope: one action or a whole move

The results above lock each *action* (~0.2 s). The design holds a path until
the read-back confirms an edit, and in these traces a state-changing edit is a
*move* of several steps: open the editor, think, type, think, save. Of 76 moves
in the decomposed runs, all took more than one step (2.8 on average) and the
median window from the move's first step to its write, thinking included, was
3.8 s. The simulator now has a `scope` per policy (`action` or `move`); under
`move` all of a move's holds are taken at its first step and released when its
writing step ends. Moves are cut from the recorded writes (`moves_of` in
`exp1/simulate.py`). Folders: `exp1-openai-budget/simulate-lock-scope/`,
`exp1-openai-shared/simulate-lock-scope/` (per-action rows there are identical
to the earlier analyses).

Lock rule under move scope, global lock / path holds, both with target
staleness, look-again cost 0.5 x think:

| Run | 2 workers | 3 workers | 4 workers |
|-----|-----------|-----------|-----------|
| six low-contention families (12 tasks) | 1.101 [1.050, 1.157] | 1.138 [1.086, 1.194] | 1.425 [1.237, 1.645] |
| `shared_invoice` (3 tasks) | 1.168 [1.098, 1.251] | 1.256 [1.233, 1.272] | 1.713 [1.600, 1.826] |

Speedup over one worker at 4 workers (same settings):

| Policy | six families | `shared_invoice` |
|--------|--------------|------------------|
| global lock per action, target staleness | 2.48 | 3.47 |
| global lock per move, target staleness | 1.82 | 2.05 |
| path holds per move, target staleness | 2.59 | 3.52 |
| ideal (no interaction) | 2.59 | 3.52 |

Reading:

- If edits must be protected as whole moves, path-level holds are worth
  10-71% over one global lock, well over the 10% line at 3-4 workers: a global
  lock held across a move makes every other worker wait through one worker's
  thinking. This is the regime in which CoAgent and Atomix found locking costly.
- Path holds per move cost nothing here relative to per action
  (`path_move+target` / `path_board+target` = 1.000): no two jobs ever held
  the same leaf. Real same-leaf contention would make them cost something.
- Two designs reach the ideal in these traces: a global lock per *action* with
  target staleness, and path holds per *move* with target staleness. Which one
  a real system needs is a correctness question the replay cannot answer: is
  checking the target before each action enough to stop one worker's
  multi-step edit from being broken or overwritten by another's, or must the
  move be held? That needs live concurrent runs, not a replay of single-agent
  traces.

## Experiment 4: live agents at once (is a per-action lock as safe as the board?)

Four gpt-6.2-sol agents ran *at the same time* on one app, one job each, through
a live board. Nothing was replayed. Two families: `shared_invoice` (each job
edits a different field of one invoice) and `increment` (each job adds its own
amount to the *same* price, so a job that saves a value read before another
job's save loses that job's amount). Conditions: `no_board` (no coordination;
the positive control), `lock_per_action` (one global lock per action plus the
target check: the design doc's condition 2), `board_per_move` (path holds kept
from the start of an edit until its write, plus the target check: a simplified
condition 3). The target check uses ground truth, i.e. a perfect detector.

| Family | Condition | Correct final state | Wall time | Steps | Look-agains | Broken editors | Lock wait (sum) | Cost per run |
|--------|-----------|---------------------|-----------|-------|-------------|----------------|-----------------|--------------|
| `shared_invoice` | no_board | 6/6 | 19.9 s | 29.7 | 0 | 5.0 | 0 | $0.094 |
| `shared_invoice` | lock_per_action | 6/6 | 19.8 s | 29.5 | 4.3 | 1.2 | 2.4 s | $0.094 |
| `shared_invoice` | board_per_move | 6/6 | 19.0 s | 28.8 | 4.7 | 0.2 | 0.6 s | $0.092 |
| `increment` | no_board | **1/6** | 27.2 s | 32.3 | 0 | 2.5 | 0 | $0.112 |
| `increment` | lock_per_action | 6/6 | 30.9 s | 38.7 | 6.3 | 0 | 2.7 s | $0.137 |
| `increment` | board_per_move | 6/6 | 32.7 s | 29.2 | 7.8 | 0 | 27.1 s | $0.099 |

Means per run of 4 agents. No wrong-target clicks, collateral changes, lease
expiries or errors in any run.

Reading:

- The hazard is real: without coordination 5 of 6 `increment` runs lost
  updates (e.g. two agents both read $150.50, one saved +$46, the other then
  saved +$42 over it).
- Both designs kept every final state correct: 12/12 each (95% Wilson lower
  bound 76%, so a failure rate up to about 1 in 4 is not ruled out).
- They keep it correct in different ways. Under `lock_per_action` the target
  check catches each stale value and the agent re-reads and redoes the edit
  (38.7 steps per run on `increment`). Under `board_per_move` the other agents
  wait while one holds the price through its edit (27 s of waiting per run),
  and nobody redoes work (29.2 steps): 25% fewer steps and 28% lower cost on
  same-field work.
- Wall time is the same: lock_per_action time / board time = 1.00 [0.93, 1.07]
  over all 12 paired runs (1.05 on `shared_invoice`, 0.94 on `increment`).
- Not measured, but it follows from the mechanism: `lock_per_action` is safe
  only as long as the target check catches every conflicting change, while
  the board prevents same-field conflicts by holding, whether or not a check
  fires. With a real detector (Experiment 2: every change named in 96% of
  actions) that difference could decide safety.

Limits: 6 tasks, 2 repetitions, one app, one model; the target check is
perfect; `board_per_move` lacks the doc's move classes, older-job-wins and
fork rules; 6 escalations on `shared_invoice` (a worker mid-move needed a path
another held, e.g. the Save button stands for the whole invoice).

## Experiment 4b: the same, with a realistic change detector

Experiment 4's target check read ground truth. Here it is the Experiment 2
observer (page model plus network replies) running in each agent's own tab, so
it knows only what has reached that page. `increment` tasks only, 6 tasks x 2
repetitions.

| Condition | Correct final price | Wall time | Steps | Look-agains | Lock wait (sum) | Cost per run |
|-----------|---------------------|-----------|-------|-------------|-----------------|--------------|
| lock_per_action_observed | **3/12** | 39.5 s | 37.0 | 0.7 | 1.4 s | $0.127 |
| board_per_move_observed | **12/12** | 46.6 s | 28.7 | 4.5 | 38.3 s | $0.096 |

One-sided Fisher exact test, 12/12 vs 3/12: p = 0.0002.

All 9 failures have one cause, read from what each agent typed: two agents
added their amounts to the *same* old price, so one amount was lost. Under the
board every agent built on the latest price in every run.

The detector stopped far fewer actions than the perfect check (0.7 vs 6.3
look-agains per run with `lock_per_action`). Likely reasons, not yet
confirmed: a page learns of another agent's save only at its next refresh
(0.6 s); and a per-action check asks only whether something changed since
*this step's* look, while the price an agent types was read when it opened the
editor, steps earlier. The board protects the whole move from that read to
the save, so it does not depend on either.

## Experiment 4c: which check makes it safe, and is the board worth it? (scripted agents, $0)

Free runs with scripted agents (`pathboard.agent.scripted`): a fixed rule that
edits the way gpt-6.2-sol did in Experiment 4 (read the price in the cell, open
the editor, type price + amount, press Enter; start over if the action was not
carried out). Before each decision it waits a think time drawn from 1,389
recorded gpt-6.2-sol think times (median 2.15 s). The browser, app, board and
network delays are real. `increment` tasks only (all four agents add to the
same price), 6 tasks x 3 repetitions per condition.

Conditions:

- `no_board`: no coordination.
- `lock_page_check`: one global lock per action, plus the Experiment 2 page
  observer check (as `lock_per_action_observed` in 4b).
- `board_page_check`: path holds per move, same page check (as
  `board_per_move_observed` in 4b).
- `lock_version_check`: one global lock per action, plus the design doc's read
  marks with versions: the value the agent's page held at this step's look
  against the latest value.
- `board_version_check`: path holds per move, with version read marks (the
  full design).
- `save_check` (baseline): one global lock per action, plus a version check at
  save time against the look at which the edit began (optimistic concurrency
  control: no hold, but a stale save is refused).

| Condition | Correct final price | Wall time | Steps | Look-agains | Lock wait (sum) |
|-----------|---------------------|-----------|-------|-------------|-----------------|
| no_board | 7/18 | 39.1 s | 38.4 | 0 | 0 s |
| lock_page_check | 13/18 | 41.6 s | 40.4 | 1.0 | 1.4 s |
| board_page_check | 9/18 | 37.6 s | 27.0 | 4.8 | 30.2 s |
| lock_version_check | **18/18** | 46.1 s | 43.9 | 1.2 | 1.6 s |
| save_check | **18/18** | 48.8 s | 48.2 | 5.9 | 1.5 s |
| board_version_check | **18/18** | **39.2 s** | **29.2** | 5.9 | 29.7 s |

`save_check` time / this time (paired): board_version_check 1.25 (95% CI
1.15-1.35); lock_version_check 1.07 (1.03-1.10). No run errored; every agent
finished. Two-sided Fisher exact tests: board_version_check vs
board_page_check p = 0.001; save_check vs no_board p = 0.0001; lock_page_check
vs board_page_check p = 0.31 (not significant).

Reading:

- Version read marks are what make it safe. Every design with them lost
  nothing; both page-check designs lost updates.
- Among the safe designs the full board is the fastest: 25% faster than the
  save-time check and 40% fewer steps. Agents under the board wait their turn
  instead of doing an edit, having it refused, and redoing it.
- Why the page checks miss (`diagnose_misses.py`, from the check log of every
  step): in every lost update, another agent saved after this agent read the
  price, and the page check did not see it because the page had not yet
  refreshed (0.6 s poll). Under the board this happens when an agent reads the
  price, waits for the hold while another agent finishes its move, and then
  acts at once on the price it read before waiting. The hold starts after the
  read; the version mark covers that gap.

Caveat: the scripted agents do not reproduce the real model's page-check
results. With gpt-6.2-sol (4b) the page check gave lock 3/12 and board 12/12;
scripted, 13/18 and 9/18 (both differences from the model significant, p =
0.02 and 0.004). So the script and the model differ in some way that matters
when the check is imperfect (when they read, or what they type after a wait);
the page-check rows here should not be taken as the model's behaviour. The
version-check rows are less sensitive to this, since that check compares with
the server's latest value whatever the agent does, but they still need a small
real-model confirmation.

## Experiment 4d: the board vs the save-time check, with real models

The real-model check of Experiment 4c. Both conditions use the same version
check (`recheck_move_version`: the price the agent's page held at the look
where the edit began, against the server's latest value), so they differ only
in the holds:

- `board_move_version`: path holds per move (the board).
- `save_check`: one global lock per action, no holds; a stale save is refused
  and the agent must start the edit over (optimistic concurrency control).

`increment` tasks (same suite as 4b), 6 tasks x 2 repetitions per model.

| Model | Design | Correct | All agents finished | Wall time | Steps | Refusals | Cost per run |
|-------|--------|---------|---------------------|-----------|-------|----------|--------------|
| gpt-6.2-sol | board_move_version | **12/12** | 12/12 | **34.4 s** | **29.0** | 6.0 | **$0.097** |
| gpt-6.2-sol | save_check | 11/12 | 12/12 | 39.3 s | 45.4 | 5.9 | $0.160 |
| gpt-6-luna | board_move_version | **10/12** | 12/12 | **27.8 s** | **29.8** | 5.8 | **$0.005** |
| gpt-6-luna | save_check | 1/12 | 10/12 | 60.0 s | 53.4 | 7.0 | $0.012 |

Paired, save_check time / board time (geometric mean over the 12 task-repetitions):
1.14 with gpt-6.2-sol (range 0.91-1.30), 2.01 with gpt-6-luna (0.93-4.08).
Two-sided Fisher exact test on correct runs: gpt-6-luna 10/12 vs 1/12,
p = 0.0006; gpt-6.2-sol 12/12 vs 11/12, p = 1.0.

What went wrong, from every agent's transcript and check log:

- No lost update got past either check: no carried-out save had a price
  change since its edit's read that the check did not refuse.
- gpt-6.2-sol, save_check, 1 failure: one agent was refused three times in a
  row (the others kept saving first), then gave up and finished without its
  change.
- gpt-6-luna, save_check, 11 failures: mistakes after refusals. Agents
  "corrected" the price back to the original price plus their own amount
  (wiping out the others' work), saved twice, or never finished.
- gpt-6-luna, board, 2 failures: the same kind of mistake after a refusal in
  the gap before the hold (one agent saved without typing a new price; one
  added its amount to the original price instead of the one it had just read).
- Under the board refusals happen only in that gap, before the hold starts.
  Under the save-time check they happen in the middle of an edit, after the
  agent has typed, and the agent must notice, cancel and redo it.

Reading: with a strong model both are safe and the board is modestly faster
and about 40% cheaper; with a weak model the save-time check's refusals cause
the agent's own mistakes, and the board is much more reliable (10/12 vs 1/12)
and twice as fast.

How the save-time check was made fair (each found by a smoke test or a run,
fixed, committed, and the condition re-run; the earlier runs are kept):

1. A refused save had ended the edit's window, so pressing Enter again saved
   the stale value. Now a refusal stays until the editor is closed, and Escape
   is never refused (commit 697acf5).
2. The refusal notice said only "decide again"; agents kept re-saving. Under
   these checks it now says to press Escape, read the current value and start
   the edit over (commit 6abfd60).
3. A save that landed on an editor the page had redrawn had no resolvable
   target and went unchecked (`sol/`: save_check 4/12, all lost updates).
   Now the check during an edit also covers what the action that opened it
   targeted (commit 90de140). `luna-save-rerun/` and `sol-save-rerun/` are with this fix; the
   table uses them for save_check.

The board runs (`luna/`, `sol/`) used the check before fix 3. The holds cover
that case (no other worker can save the price during the move), and no board
run lost an update.

## Experiments 4e-4g: the board without the app's server (a third-party app)

A real board cannot read a third-party app's server. Two steps take that access away.

**The registry (4e, 4f).** Writers publish versions themselves. Right after a save, the
writer publishes the app's change number for that save, taken from the app's reply to its own
page, for the price it changed. A reader's read mark is the change number its page was up to
when its edit began. A save is refused if the registry holds a later number for its target
(`recheck_move_registry`). The `_page` variant also refuses if the agent's own page now shows a
different value than when the edit began, which catches edits made outside the board once they
reach the page (`recheck_move_registry_page`). Code: `live/registry.py`, `agent/checks.py`.

**Page-only (4g).** The board and the check also stop using the testbed's hidden information
(`page_only: true`, `live/pageview.py`):
- Which price an element shows comes from what a user sees: the URL, the row's first cell and
  the column header, mapped by a small per-app adapter.
- Saves come from the app's success replies to the page.
- An edit opening or closing comes from an "Edit ..." box appearing or disappearing.
- Navigation comes from the URL changing.

The testbed's hidden information is used only to score the runs.

**Outside edits (4f, 4g).** Someone outside the board adds $7 to the same price at 3 s, 9 s and
15 s after the start. Scoring takes those additions back out.

All runs use the `increment` tasks with 4 agents on one price.

| Experiment | Agents | Condition | Correct | Wall time (median) | Steps (median) |
|---|---|---|---|---|---|
| 4e, no outside edits | scripted | board, reads server | 18/18 | 38.4 s | 30 |
| | | board, registry | 18/18 | 38.6 s | 30 |
| | | board, registry + page | 18/18 | 39.9 s | 30 |
| | | save-time check, reads server | 18/18 | 46.1 s | 44 |
| | | save-time check, registry | 18/18 | 42.7 s | 42 |
| 4f, outside edits | scripted | board, reads server | 18/18 | 55.6 s | 42 |
| | | board, registry + page | 18/18 | 54.5 s | 39 |
| | | save-time check, registry + page | 17/18 | 58.5 s | 66 |
| | | board, registry only | 15/18 | 51.7 s | 40 |
| 4g, outside edits, page-only | scripted | board, registry + page | 17/18 | 53.3 s | 39 |
| | | board, registry only | 14/18 | 51.0 s | 40 |
| 4g, outside edits, page-only | gpt-6-luna | board, registry + page | 4/12 | 38.8 s | 38 |
| | | board, registry only | 0/12 | 30.8 s | 37 |

Two-sided Fisher exact tests, registry + page vs registry only:
- 4f: p = 0.23
- 4g scripted: p = 0.34
- 4g gpt-6-luna: p = 0.09

gpt-6-luna cost $0.172 for 24 runs.

Every save, checked against ground truth (`exp4g-third-party/classify_failures.py`):

| Run | Condition | Saves | Stale saves refused | Stale saves let through |
|---|---|---|---|---|
| 4f scripted | board, registry only | 78 | 0 | 9 |
| 4f scripted | board, registry + page | 83 | 11 | 0 |
| 4g scripted | board, registry only | 77 | 0 | 9 |
| 4g scripted | board, registry + page | 82 | 10 | 1 |
| 4g gpt-6-luna | board, registry only | 54 | 0 | 17 |
| 4g gpt-6-luna | board, registry + page | 56 | 5 | 4 |

What this shows:

- **Registry vs reading the server.** Without outside edits, the registry is as safe as reading
  the server (18/18 in every condition), at the same speed. The board stays the fastest safe
  design: about 30 steps against 42-44 for the save-time check.
- **Registry only.** It never refused a stale save in any outside-edit run. Every stale save it
  let through was caused by an outside edit, which never reaches the registry.
- **Registry + page.** Stale saves it let through were saved before the outside edit reached the
  agent's page. The page polls every 0.6 s, so the edit had landed just before the save. Without
  server access this window cannot be closed; only the app itself (a conditional write) can.
  - The one 4f save-time-check miss is the same case.
  - gpt-6-luna is quicker between opening the editor and saving than the scripted agents, so
    more of its saves fall in that window: 4 of 9 stale saves against 1 of 11.
- **The board without the testbed's hidden information** stayed safe in scripted runs without
  outside edits (test suite), and 4g matches 4f. The path holds worked with no escalations and
  no lease expiries.
- **gpt-6-luna failures.** Most came from the model, not from the board:
  - 4 runs per condition had an agent save twice. It saved, then saw a price that others had
    changed since, decided its save had failed, and added its amount again.
  - Some agents finished without saving: one claimed the new price was on the page when it was
    not.
  - Neither mistake can be stopped by checking reads: the agent cannot tell its own change apart
    from others' changes on the page. A board that tells the agent "your save went through"
    (it already knows from the app's reply) is the next thing to try.

What still depends on this app: the adapter that maps an element to a data path (URL, row key,
column header). It needs only what a user sees, but it has to be written for each app.

## Against a single agent (same tasks, same repetitions)

One agent did each whole task alone (`exp4-openai-single/`): 24/24 correct.

| Family | Design | Correct | Wall time | One agent's time / this time (paired) | Cost per run |
|--------|--------|---------|-----------|----------------------------------------|--------------|
| `shared_invoice` | one agent | 12/12 | 40.1 s | 1 | $0.052 |
| `shared_invoice` | lock_per_action | 6/6 | 19.8 s | 2.01 | $0.094 |
| `shared_invoice` | board_per_move | 6/6 | 19.0 s | 2.10 | $0.092 |
| `increment` | one agent | 12/12 | 46.9 s | 1 | $0.055 |
| `increment` | board_per_move (perfect check) | 6/6 | 32.7 s | 1.46 | $0.099 |
| `increment` | board_per_move_observed | 12/12 | 46.6 s | 1.00 | $0.096 |
| `increment` | lock_per_action_observed | 3/12 | 39.5 s | 1.18 | $0.127 |

Reading:

- Where jobs touch different fields, four agents under the board finish about
  2.1x faster than one agent, equally correct, at about 1.8x the token cost.
- Where every job edits the same field, the board makes the agents take turns:
  with the realistic detector it is no faster than one agent (1.00x) and costs
  more. The simpler lock is faster but loses updates.
- So the board is as safe as a single agent and safer than a per-action lock
  under realistic detection; it is faster than one agent only when the work
  can actually overlap, as the design doc's claim predicts.

## Experiment 3 (worker robustness, interference while the agent decides)

All 12 episodes succeeded; no collateral changes.

| Condition | wrong-target actions | stale-element errors | look-again stops |
|-----------|----------------------|----------------------|------------------|
| no interference | 0 | 0 | 0 |
| list re-sorted, positional handles | 1 | 2 | 0 |
| same, plus "your target changed, look again" notice | 0 | 0 | 4 |

The one wrong-target action is paired across conditions (same task, same step,
same click): without the notice the click opened the editor on INV-1036
instead of INV-1013 with no error; with the notice the click was stopped. The
agent recovered in the first case, so it did not change the outcome here.
Four episodes per condition cannot measure rates; this is an existence proof.

## Agent change during these runs

The first OpenAI attempt (stopped, not used) showed the agent looping on
early-discount tasks: it read a rate on the Vendors page, left, and had no
record of it. Since commit "Agent memory: show the agent its own earlier
notes", each history entry carries the agent's note. All results above use
that agent.
