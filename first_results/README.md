# First results

Small runs, chosen to fit a ~$7 API budget. They show direction, not
statistically settled effects. Every folder holds the run's own `manifest.json`
(config, git commit, package versions).

| Folder | What | Model | Size |
|--------|------|-------|------|
| `exp2-sandbox-run/` | Experiment 2, observer precision | none needed | 27 action types x 5 instances x 3 render modes |
| `exp1-openai-budget/` | Experiment 1, headroom (`collect/` traces, `simulate/` analysis) | gpt-6.2-sol, reasoning medium | 6 families x 2 tasks x 1 repetition, both modes: 24 episodes, all successful |
| `exp1-openai-shared/` | Experiment 1 on the contended family `shared_invoice` | gpt-6.2-sol, reasoning medium | 3 tasks x 1 repetition, both modes: 6 episodes, all successful |
| `exp4-openai-budget/` | Experiment 4, live: all jobs of a task at once, one agent each | gpt-6.2-sol, reasoning medium | 6 tasks x 3 conditions x 2 repetitions: 36 runs of 4 agents (plus a 3-run smoke test) |
| `exp3-openai-budget/` | Experiment 3, worker robustness (`run/` episodes, `summary/` re-summary) | gpt-6.2-sol, reasoning medium | 4 families x 1 task x 3 conditions: 12 episodes, all successful |

Total API spend for the OpenAI runs, including a stopped first attempt and two
one-episode smoke tests: at most $6.11 for Experiments 1 and 3, and at most
$4.12 for Experiment 4 (a later credit).

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
