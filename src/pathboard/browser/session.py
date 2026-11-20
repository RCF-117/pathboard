"""Tabs: open the testbed in a real browser, read it as text, act on it.

A ``Tab`` offers two separate surfaces:

* the agent surface (``snapshot`` and ``act``), which uses only what is visible
  on the page, and
* the ground-truth surface (methods starting with ``gt_``), which reads the
  testbed's instrumentation and is for the experiment harness only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from .page_scripts import INIT_SCRIPT, SNAPSHOT_SCRIPT

HandleMode = Literal["ref", "positional"]
AGENT_ACTIONS = ("click", "fill", "select", "press", "back", "wait")
_KEYS = ("Enter", "Escape", "Tab")


@dataclass(frozen=True)
class BrowserConfig:
    headless: bool
    viewport_width: int
    viewport_height: int
    settle_quiet_ms: int  # page counts as settled after this long with no activity
    settle_timeout_ms: int  # failing to settle within this is a harness error
    action_timeout_ms: int  # per browser action


@dataclass(frozen=True)
class Handle:
    id: str
    kind: str
    label: str


@dataclass(frozen=True)
class Snapshot:
    text: str
    handles: tuple[Handle, ...]


@dataclass(frozen=True)
class ActResult:
    outcome: str  # ok | stale_element | action_error
    error: str | None


class Tab:
    def __init__(self, page: Page, base_url: str, label: str, actor: str, cfg: BrowserConfig, handle_mode: HandleMode):
        if handle_mode not in ("ref", "positional"):
            raise ValueError(f"unknown handle_mode {handle_mode!r}")
        self.page = page
        self.base_url = base_url
        self.label = label  # used in view paths: view/<label>/...
        self.actor = actor  # sent as X-Actor so the server can attribute writes
        self.cfg = cfg
        self.handle_mode = handle_mode
        page.set_default_timeout(cfg.action_timeout_ms)
        page.set_extra_http_headers({"X-Actor": actor})
        page.add_init_script(INIT_SCRIPT)

    # ---- lifecycle -------------------------------------------------------
    def open(self, path: str) -> None:
        self.page.goto(self.base_url + path)
        self.page.wait_for_function("window.__gt && window.__gt.ready === true")
        self.settle()

    def settle(self) -> None:
        ok = self.page.evaluate("([q, t]) => window.__sw.settle(q, t)",
                                [self.cfg.settle_quiet_ms, self.cfg.settle_timeout_ms])
        if not ok:
            raise RuntimeError(f"tab {self.label}: page did not settle within {self.cfg.settle_timeout_ms} ms")

    # ---- agent surface -----------------------------------------------------
    def snapshot(self) -> Snapshot:
        raw = self.page.evaluate(SNAPSHOT_SCRIPT, self.handle_mode)
        return Snapshot(raw["text"], tuple(Handle(**h) for h in raw["handles"]))

    def _resolve(self, handle_id: Any):
        if not isinstance(handle_id, str):
            return None
        return self.page.evaluate_handle("id => window.__sw.resolve(id)", handle_id).as_element()

    def act(self, action: str, args: dict[str, Any]) -> ActResult:
        """Run one agent action and wait for the page to settle.

        A bad request from the agent is data, not a harness failure, so it is
        returned as an ``action_error`` outcome rather than raised.
        """
        if action not in AGENT_ACTIONS:
            return ActResult("action_error", f"unknown action {action!r}")
        try:
            if action == "back":
                self.page.evaluate("history.back()")
            elif action == "wait":
                self.page.wait_for_timeout(1000)
            elif action == "press" and "id" not in args:
                if args.get("key") not in _KEYS:
                    return ActResult("action_error", f"key must be one of {_KEYS}")
                self.page.keyboard.press(args["key"])
            else:
                el = self._resolve(args.get("id"))
                if el is None:
                    return ActResult("stale_element", f"element {args.get('id')!r} is no longer on the page")
                if action == "click":
                    el.click()
                elif action == "fill":
                    if not isinstance(args.get("text"), str):
                        return ActResult("action_error", "fill needs a 'text' string")
                    el.fill(args["text"])
                elif action == "select":
                    if not isinstance(args.get("option"), str):
                        return ActResult("action_error", "select needs an 'option' string")
                    el.select_option(label=args["option"])
                elif action == "press":
                    if args.get("key") not in _KEYS:
                        return ActResult("action_error", f"key must be one of {_KEYS}")
                    el.press(args["key"])
        except PlaywrightError as exc:
            self.settle()
            return ActResult("action_error", str(exc).splitlines()[0][:300])
        self.settle()
        return ActResult("ok", None)

    # ---- ground-truth surface (harness only) --------------------------------
    def gt_key_of(self, handle_id: str) -> str | None:
        """Identity of the element a handle currently resolves to."""
        return self.page.evaluate(
            "id => { const el = window.__sw.resolve(id); return el ? window.__gt.keyOf(el) : null; }", handle_id)

    def gt_paths_of(self, handle_id: str) -> tuple[str, ...]:
        paths = self.page.evaluate(
            "id => { const el = window.__sw.resolve(id); return el ? window.__gt.pathsOf(el) : []; }", handle_id)
        return tuple(self._tabbed(p) for p in paths)

    def gt_handle_keys(self, handles: tuple[Handle, ...]) -> dict[str, str | None]:
        ids = [h.id for h in handles]
        keys = self.page.evaluate(
            "ids => ids.map(id => { const el = window.__sw.resolve(id); return el ? window.__gt.keyOf(el) : null; })", ids)
        return dict(zip(ids, keys))

    def gt_displayed(self) -> tuple[str, ...]:
        """Data leaves currently shown on the page."""
        return tuple(self.page.evaluate("() => window.__gt.displayed()"))

    def gt_view_leaves(self) -> dict[str, str]:
        raw = self.page.evaluate("() => window.__gt.viewLeaves()")
        return {f"view/{self.label}/{k}": v for k, v in raw.items()}

    def gt_wait_applied(self, seq: int) -> None:
        """Wait until the page has pulled every server change up to ``seq``."""
        self.page.wait_for_function("s => window.__gt.seqApplied() >= s", arg=seq)
        self.settle()

    def gt_find(self, key: str):
        el = self.page.evaluate_handle("k => window.__gt.find(k)", key).as_element()
        if el is None:
            raise LookupError(f"tab {self.label}: no element with ground-truth key {key!r} on {self.page.url}")
        return el

    def gt_has(self, key: str) -> bool:
        return bool(self.page.evaluate("k => window.__gt.find(k) !== null", key))

    def _tabbed(self, path: str) -> str:
        return f"view/{self.label}/{path[5:]}" if path.startswith("view/") else path


class BrowserSession:
    """One Chromium instance.  Each tab gets its own browser context."""

    def __init__(self, cfg: BrowserConfig):
        self.cfg = cfg
        self._pw = None
        self._browser = None

    def __enter__(self) -> "BrowserSession":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.cfg.headless)
        return self

    def __exit__(self, *exc: Any) -> None:
        self._browser.close()
        self._pw.stop()

    def new_tab(self, base_url: str, label: str, actor: str, handle_mode: HandleMode) -> Tab:
        ctx = self._browser.new_context(
            viewport={"width": self.cfg.viewport_width, "height": self.cfg.viewport_height})
        return Tab(ctx.new_page(), base_url, label, actor, self.cfg, handle_mode)

    def close_tab(self, tab: Tab) -> None:
        tab.page.context.close()
