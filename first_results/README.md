# First results

Small runs, chosen to fit a ~$7 API budget. They show direction, not
statistically settled effects. Every folder holds the run's own `manifest.json`
(config, git commit, package versions).

| Folder | What | Model | Size |
|--------|------|-------|------|
| `exp2-sandbox-run/` | Experiment 2, observer precision | none needed | 27 action types x 5 instances x 3 render modes |
| `exp1-openai-budget/` | Experiment 1, headroom (`collect/` traces, `simulate/` analysis) | gpt-6.2-sol, reasoning medium | 6 families x 2 tasks x 1 repetition, both modes: 24 episodes, all successful |
| `exp3-openai-budget/` | Experiment 3, worker robustness (`run/` episodes, `summary/` re-summary) | gpt-6.2-sol, reasoning medium | 4 families x 1 task x 3 conditions: 12 episodes, all successful |

Total API spend for the OpenAI runs, including a stopped first attempt and two
one-episode smoke tests: at most $5.76.

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

Why the lock rarely matters: an action holds the lock for 0.2 s out of a ~1.9 s
step. What does matter is the staleness rule (when an agent must look again).

**Limit that matters most:** the authored job splits never have two jobs
writing the same stored leaf (write-write overlap 0 in every family), and two
families are fully ordered (`chain`) or fan-in (`approve_pay`). The path board
was not tested on contended work. A contended task family, `shared_invoice`,
has since been added (all jobs on one invoice); it has not been run with a model yet.

Re-analysing this run with `global_lock+target` added splits the small gains:
the lock rule (global vs path, both with target staleness) gives 1.003-1.043;
the staleness rule (page vs target, both under one lock) gives 1.00-1.03.

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
