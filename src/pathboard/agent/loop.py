"""One agent episode: observe, decide, act, repeat; with ground-truth recording.

The agent sees only the text view of the page.  Everything recorded about what
a step really read and wrote comes from the ground-truth probe and is never
shown to the model.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from collections.abc import Callable

from ..browser.groundtruth import GroundTruthProbe
from .checks import MITIGATIONS, ActionCheck, Mitigation
from ..browser.session import AGENT_ACTIONS, Tab
from ..llm.base import LLMClient, LLMReply
from ..runlog import append_jsonl
from ..testbed.server import TestbedServer
from ..trace import EpisodeTrace, StepRecord


SYSTEM_PROMPT = """You operate a web application called Ledger. Each turn you get the task, the actions you \
have taken so far, and a text rendering of the current page. You reply with exactly one action.

Reading the page:
- Interactive elements look like [e12 button "Save changes"]. The first word is the element id.
- Element ids are valid only for the page shown in the current message. Never reuse an id from memory.
- Table rows are shown as | cell | cell | ... |.

Actions:
- click: click an element (id).
- fill: replace the content of an input (id, text).
- select: choose an option of a select by its visible text (id, option).
- press: press Enter, Escape or Tab, on an element (id) or on whatever has focus.
- back: go back one page.
- wait: wait a moment for the page.
- finish: the task is complete. If the task asks for an answer, put it in "answer".

How Ledger works:
- A table cell shown as [eN cell "..."] can be edited: click it and an input or a select appears in that cell. \
For an input, fill it and then press Enter to save. For a select, choosing an option saves immediately. \
Escape cancels.
- On an invoice page, the form at the top is saved only when you click "Save changes". Line items save \
immediately.
- The invoice list is paged; use the search box to find an invoice by id.

Your notes:
- Each action carries a short note. Your earlier notes are listed with your actions under ACTIONS \
SO FAR; this is your only memory of pages you are no longer on.
- When you read a value you will need on a later page (a rate, an amount, an id), write it in the note.

Rules:
- Change only what the task asks for.
- Before you finish, check on the page that your changes are really there.
"""

ACT_TOOL: dict[str, Any] = {
    "name": "act",
    "description": "Take one action in the Ledger app.",
    "input_schema": {
        "type": "object",
        "properties": {
            "note": {
                "type": "string",
                "description": (
                    "One short sentence: what you are doing and why, plus any values you "
                    "read that you will need later. Shown back to you in later turns."
                ),
            },
            "action": {"type": "string", "enum": [*AGENT_ACTIONS, "finish"]},
            "id": {
                "type": "string",
                "description": "Element id such as e12 (click, fill, select, optional for press).",
            },
            "text": {"type": "string", "description": "Text for fill."},
            "option": {"type": "string", "description": "Visible option text for select."},
            "key": {
                "type": "string",
                "enum": ["Enter", "Escape", "Tab"],
                "description": "Key for press.",
            },
            "answer": {
                "type": "string",
                "description": "Final answer for finish, if the task asks for one.",
            },
        },
        "required": ["note", "action"],
    },
}


class Gate(Protocol):
    """A coordinator between the agent and the workspace (Experiment 4's live board).

    ``before_act`` is called after the model has decided and just before the target check and
    the action; it may block until the move is allowed.  Exactly one of ``after_act`` (the
    action ran) or ``skipped`` (the target check stopped it) follows.
    """

    def before_act(self, action: str, target_paths: tuple[str, ...]) -> None: ...

    def after_act(self, view_writes: set[str], data_writes: set[str]) -> None: ...

    def skipped(self) -> None: ...


@dataclass(frozen=True)
class AgentConfig:
    max_steps: int  # the episode is cut off (unfinished) after this many steps
    history_actions: int  # how many of the most recent actions are listed in the prompt


def build_prompt(
    goal: str, history: list[str], page_text: str, notice: str | None, keep: int
) -> str:
    shown = history[-keep:] if keep > 0 else []
    skipped = len(history) - len(shown)
    lines = ["TASK", goal, "", "ACTIONS SO FAR"]
    if skipped:
        lines.append(f"({skipped} earlier actions not shown)")
    lines += shown or ["(none yet)"]
    if notice:
        lines += ["", "NOTICE", notice]
    lines += ["", "CURRENT PAGE", page_text]
    return "\n".join(lines)


def _with_note(entry: str, note: Any) -> str:
    """A history entry followed by the agent's own note: its only memory of earlier pages."""
    if isinstance(note, str) and note.strip():
        return f"{entry} | note: {note.strip()}"
    return entry


def _describe(action: str, args: dict[str, Any], labels: dict[str, str]) -> str:
    bits = [action]
    if "id" in args:
        bits.append(f"{args['id']} ({labels.get(args['id'], 'unknown element')})")
    for k in ("text", "option", "key", "answer"):
        if k in args:
            bits.append(f"{k}={args[k]!r}")
    return " ".join(bits)


@dataclass(frozen=True)
class _Turn:
    """What one step observed and decided, before the outcome is known."""

    index: int
    observe_s: float
    think_s: float
    action: str
    args: dict[str, Any]
    reads: tuple[str, ...]
    reply: LLMReply

    def record(
        self,
        outcome: str,
        error: str | None,
        act_s: float,
        writes: set[str],
        target: tuple[str, ...],
        intended: str | None,
        actual: str | None,
    ) -> StepRecord:
        return StepRecord(
            index=self.index,
            observe_s=self.observe_s,
            think_s=self.think_s,
            act_s=act_s,
            action=self.action,
            action_args=self.args,
            outcome=outcome,
            error=error,
            reads_view=self.reads,
            reads_target=target,
            writes=tuple(sorted(writes)),
            intended_target=(intended,) if intended else (),
            actual_target=(actual,) if actual else (),
            input_tokens=self.reply.input_tokens,
            output_tokens=self.reply.output_tokens,
        )


def _log(path: Path | None, record: dict[str, Any]) -> None:
    if path is not None:
        append_jsonl(path, record)


def run_episode(
    tab: Tab,
    server: TestbedServer,
    llm: LLMClient,
    goal: str,
    task_id: str,
    job_id: str,
    repetition: int,
    cfg: AgentConfig,
    mitigation: Mitigation,
    stale_window_hook: Callable[[int, str | None], None] | None,
    transcript_path: Path | None,
    before_observe_hook: Callable[[int], None] | None = None,
    gate: Gate | None = None,
) -> EpisodeTrace:
    """Run one episode in an already opened tab.

    ``stale_window_hook(step_index, target_key)`` is called after the model has
    decided and before the action runs, i.e. inside the window in which the
    agent's view of the page can go stale.  Experiment 3 uses it to let a
    scripted interferer change the page at exactly that moment.
    ``before_observe_hook(step_index)`` is called just before the agent looks
    at the page; a change made there is the control case (same change, but the
    agent sees it).

    ``mitigation`` stands in for a coordinator that tells the agent when
    something changed after it looked:
      none            act regardless
      recheck_any     if anything at all changed, do not act; look again
      recheck_target  look again only if the change touches the element the
                      agent is about to use (or that element is no longer the
                      one it saw)
    The change test uses ground truth, so it measures how the agent copes with
    perfect notification, not how good a real detector is.
    """
    if mitigation not in MITIGATIONS:
        raise ValueError(f"unknown mitigation {mitigation!r}")
    probe = GroundTruthProbe(tab, server)
    check = ActionCheck(mitigation, tab, probe)
    history: list[str] = []
    steps: list[StepRecord] = []
    notice: str | None = None
    finished, answer = False, None
    started = time.monotonic()
    for index in range(cfg.max_steps):
        if before_observe_hook is not None:
            before_observe_hook(index)  # a change here is seen by the agent: not stale
        t0 = time.monotonic()
        snap = tab.snapshot()
        observe_s = time.monotonic() - t0
        reads = probe.reads()
        keys = tab.gt_handle_keys(snap.handles)
        labels = {h.id: f'{h.kind} "{h.label}"' for h in snap.handles}
        seen = probe.mark()
        check.look(seen)

        prompt = build_prompt(goal, history, snap.text, notice, cfg.history_actions)
        notice = None
        t1 = time.monotonic()
        reply = llm.decide(SYSTEM_PROMPT, prompt, ACT_TOOL)
        think_s = time.monotonic() - t1
        raw = reply.tool_input
        action = raw.get("action")
        note = raw.get("note")
        args = {k: raw[k] for k in ("id", "text", "option", "key", "answer") if k in raw}
        where = {"task": task_id, "job": job_id, "repetition": repetition, "step": index}
        _log(transcript_path, {**where, "prompt": prompt, "reply": raw})
        turn = _Turn(index, observe_s, think_s, str(action), args, reads, reply)

        intended = keys.get(args["id"]) if isinstance(args.get("id"), str) else None
        if stale_window_hook is not None:
            stale_window_hook(index, intended)

        if action == "finish":
            finished, answer = True, args.get("answer")
            steps.append(turn.record("finish", None, 0.0, set(), (), None, None))
            break
        if action not in AGENT_ACTIONS:
            error = f"unknown action {action!r}"
            steps.append(turn.record("action_error", error, 0.0, set(), (), None, None))
            history.append(_with_note(f"{index + 1}. (invalid action {action!r}) -> error", note))
            continue

        has_target = isinstance(args.get("id"), str)
        actual = tab.gt_key_of(args["id"]) if has_target else None
        target_paths = tab.gt_paths_of(args["id"]) if has_target else ()
        if gate is not None:
            gate.before_act(action, target_paths)
        closes_edit = action == "press" and args.get("key") == "Escape"
        stopped, check_log = check.stop(
            has_target, intended, actual, target_paths, closes_edit=closes_edit
        )
        if mitigation != "none":
            _log(transcript_path, {**where, "check": check_log})
        if stopped:
            if gate is not None:
                gate.skipped()
            steps.append(turn.record("rechecked", None, 0.0, set(), target_paths, intended, actual))
            history.append(
                _with_note(
                    f"{index + 1}. {_describe(action, args, labels)} -> not done: the page changed first",
                    note,
                )
            )
            notice = (
                "The page changed after you last looked at it, so your last action was NOT "
                "carried out. Look at the current page and decide again."
            )
            continue

        before = probe.mark()
        t2 = time.monotonic()
        result = tab.act(action, args)
        act_s = time.monotonic() - t2
        data_writes = probe.data_writes_since(before, tab.actor)
        view_writes = probe.view_writes_since(before)
        writes = data_writes | view_writes
        check.acted(view_writes, data_writes)
        if gate is not None:
            gate.after_act(view_writes, data_writes)
        steps.append(
            turn.record(result.outcome, result.error, act_s, writes, target_paths, intended, actual)
        )
        history.append(
            _with_note(
                f"{index + 1}. {_describe(action, args, labels)} -> {result.outcome}"
                + (f": {result.error}" if result.error else ""),
                note,
            )
        )

    return EpisodeTrace(
        task_id=task_id,
        job_id=job_id,
        repetition=repetition,
        agent=llm.name,
        timing_source="measured",
        tab=tab.label,
        steps=tuple(steps),
        finished=finished,
        answer=answer,
        wall_s=time.monotonic() - started,
    )
