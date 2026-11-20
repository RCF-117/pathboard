"""Why page checks lose updates: classify the colliding saves of each failed run.

Run from this folder: python diagnose_misses.py
A collision is two carried-out saves built on the same price read.  For each such save, it
reports whether ground truth shows another worker changing the price after the read (from the
look at which the agent read the price to the save), which the page check failed to catch.
"""

import ast
import collections
import glob
import json
import re

outcome = {}
for line in open("traces.jsonl"):
    r = json.loads(line)
    t = r["trace"]
    for s in t["steps"]:
        outcome[(t["task_id"], int(t["repetition"]), r["condition"], t["job_id"], int(s["index"]))] = s["outcome"]

result = collections.Counter()
for row in map(json.loads, open("rows.jsonl")):
    task, rep, cond = row["task"], row["repetition"], row["condition"]
    if row["success"] or cond not in ("board_page_check", "lock_page_check"):
        continue
    saves = []
    for f in glob.glob(f"transcripts/{task}_r{rep}_{cond}_w*.jsonl"):
        steps, checks = {}, {}
        for rec in map(json.loads, open(f)):
            k = int(rec["step"])
            if "reply" in rec:
                steps[k] = rec
            if "check" in rec:
                c = rec["check"]
                checks[k] = c if isinstance(c, dict) else ast.literal_eval(c)
        move = None
        for k in sorted(steps):
            reply = steps[k]["reply"]
            reply = reply if isinstance(reply, dict) else ast.literal_eval(reply)
            check = checks.get(k, {})
            m = re.match(r"price \$([\d,.]+)", reply.get("note", ""))
            if m:
                move = {"read": m.group(1), "changed": False}
            if move is not None and check:
                move["changed"] |= bool(check.get("target_changed_since_look"))
            if reply.get("key") == "Enter" and move is not None:
                if not check.get("stopped") and outcome.get((task, rep, cond, steps[k]["job"], k)) == "ok":
                    saves.append(move)
                move = None
    by_read = collections.defaultdict(list)
    for s in saves:
        by_read[s["read"]].append(s)
    for group in by_read.values():
        if len(group) > 1:
            for s in group:
                kind = "price changed after the read, check missed it" if s["changed"] else "first save on that price"
                result[(cond, kind)] += 1
for k, v in sorted(result.items()):
    print(*k, v, sep=" | ")
