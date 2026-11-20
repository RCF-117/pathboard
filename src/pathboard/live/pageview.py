"""The board's view of a third-party app: only what a browser extension in the agent's tab sees.

No server access and no testbed instrumentation: the board learns everything it needs from the
agent's own page.

* Which data an element shows (``target_paths``): from the page's visible structure, the URL,
  the table row's first cell and the column header, mapped to data paths by a small per-app
  adapter (``LEDGER``).  A real deployment would need one adapter per app; writing it needs only
  what a user sees.
* Whether an action saved: the app replied to a non-GET request from this page with success.
* Whether it opened or closed an edit: an editor (an input or select labelled "Edit ...") appeared
  or disappeared on the page.
* Whether it navigated: the URL path changed.

These are turned into the same path sets the board and the check take from the harness's ground
truth, so both run unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..browser.session import Tab

_TARGET_JS = """id => {
  const el = window.__sw.resolve(id);
  if (!el) return null;
  const td = el.closest('td');
  const tr = td && td.closest('tr');
  const table = tr && tr.closest('table');
  if (!td || !table) return {url: location.pathname, header: null, row: null};
  const ths = table.querySelectorAll('thead th');
  const th = ths[td.cellIndex];
  const first = tr.querySelector('td');
  return {url: location.pathname, header: th ? th.textContent.trim() : null,
          row: first ? first.textContent.trim() : null};
}"""

_STATE_JS = """() => ({
  url: location.pathname,
  editing: document.querySelector('input[aria-label^="Edit "], select[aria-label^="Edit "]') !== null,
  saves: window.__sw.netLog.filter(e => e.method !== 'GET' && e.status >= 200 && e.status < 300).length,
})"""

# The ledger app's adapter: invoice page URL + line table column -> data path.
_INVOICE_URL = re.compile(r"^/invoices/(INV-\d+)$")
_LINE_ROW = re.compile(r"^L\d+$")
LEDGER = {"Description": "description", "Qty": "qty", "Unit price": "unit_price_cents"}


def target_paths(tab: Tab, handle_id: str) -> tuple[str, ...]:
    """The data paths the element shows, from the page alone; () if the adapter cannot tell."""
    got = tab.page.evaluate(_TARGET_JS, handle_id)
    if got is None or got["header"] is None:
        return ()
    m = _INVOICE_URL.match(got["url"])
    field = LEDGER.get(got["header"])
    if m is None or field is None or not _LINE_ROW.match(got["row"] or ""):
        return ()
    return (f"data/invoice/{m.group(1)}/line/{got['row']}/{field}",)


@dataclass(frozen=True)
class PageState:
    url: str
    editing: bool
    saves: int


def state(tab: Tab) -> PageState:
    s = tab.page.evaluate(_STATE_JS)
    return PageState(s["url"], bool(s["editing"]), int(s["saves"]))


def writes_between(tab: Tab, before: PageState, after: PageState) -> tuple[set[str], set[str]]:
    """(view writes, data writes) as the board and the check expect them, seen from the page."""
    view: set[str] = set()
    if after.url != before.url:
        view.add(f"view/{tab.label}/route")
    if after.editing != before.editing:
        view.add(f"view/{tab.label}/editing")
    data = {"data/page/saved"} if after.saves > before.saves else set()
    return view, data
