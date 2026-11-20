"""Classify each run of a results directory: safe, a worker never saved, a worker saved twice,
or every worker saved once; and count saves whose edit's read was stale (ground truth) that the
check let through or refused.  Usage: python classify_failures.py <run dir>"""

import collections
import glob
import json
import sys

d = sys.argv[1]
rows = {(r["task"], r["repetition"], r["condition"]): r for r in map(json.loads, open(d + "/rows.jsonl"))}
saves = collections.defaultdict(dict)
for line in open(d + "/traces.jsonl"):
    x = json.loads(line)
    t = x["trace"]
    n = sum(
        1
        for s in t["steps"]
        if any(w.startswith("data/") and w.endswith("unit_price_cents") for w in s["writes"])
    )
    saves[(t["task_id"], t["repetition"], x["condition"])][t["job_id"]] = n
kinds = collections.Counter()
for k, r in rows.items():
    s = saves[k]
    if r["success"]:
        kind = "safe"
    elif any(n == 0 for n in s.values()):
        kind = "a worker never saved"
    elif any(n > 1 for n in s.values()):
        kind = "a worker saved twice"
    else:
        kind = "every worker saved once"
    kinds[(k[2], kind)] += 1
for (c, kind), n in sorted(kinds.items()):
    print(f"{c:22s} {kind:26s} {n}")
c = collections.Counter()
for f in glob.glob(d + "/transcripts/*.jsonl"):
    last = None
    for line in open(f):
        x = json.loads(line)
        if "reply" in x:
            last = x["reply"]
        elif "check" in x and last and last.get("action") == "press" and last.get("key") == "Enter":
            k = x["check"]
            cond = f.rsplit("/", 1)[1].split("_r", 1)[1].split("_", 1)[1].rsplit("_", 1)[0]
            c[(cond, "saves")] += 1
            if k["stopped"]:
                c[(cond, "stale save refused")] += 1
            elif k["target_changed_since_move"]:
                c[(cond, "stale save let through")] += 1
for (cond, what), n in sorted(c.items()):
    print(f"{cond:22s} {what:26s} {n}")
