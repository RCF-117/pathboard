"""Observers: code that works out which paths an action changed.

This is the piece of the design under test in Experiment 2.  An observer sees
only what a browser-level tool could see (the visible page, DOM mutations, the
page's API traffic).  It never reads the ground-truth instrumentation.

Three sources are implemented:

``mutation_anchor``
    Take each DOM mutation and name the smallest labelled region around it,
    using the page's ``data-*`` attributes.  Needs no knowledge of the app's
    layout, but can only be as precise as the page's own updates.
``model_diff``
    A hand-written adapter for the Ledger app reads the visible page into a
    ``path -> shown text`` model before and after, and reports the paths whose
    text differs.  Independent of how the page redraws itself.
``network``
    Rebuild the app's records from its API replies and report the leaves that
    differ.  Sees data the page does not show, but never sees view state.
"""

from __future__ import annotations

from typing import Any, Literal

from ..paths import minimal_cover
from .session import Tab

Source = Literal["mutation_anchor", "model_diff", "network"]
SOURCES: tuple[str, ...] = ("mutation_anchor", "model_diff", "network")

# ---- model_diff: Ledger adapter (visible structure only) -----------------------
# Returns {leaves: {path: text}, page: <page kind>}.  View paths are returned
# without the tab label ("view/...").
_MODEL_SCRIPT = r"""
() => {
  const out = {};
  const txt = (el) => (el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : '');
  const parts = location.pathname.split('/').filter(Boolean);
  const tables = [...document.querySelectorAll('main table')];
  const bodyRows = (t) => [...t.tBodies[0].rows].map((r) => [...r.cells]);
  const heads = (t) => [...t.tHead.rows[0].cells].map((c) => txt(c));
  out['view/route'] = location.pathname;
  out['view/modal'] = txt(document.querySelector('[role=dialog] h2'));
  let page = 'other';
  if (parts[0] === 'invoices' && parts.length === 1 && tables.length) {
    page = 'invoices';
    const H = heads(tables[0]);
    const map = { Vendor: 'vendor_id', Date: 'date', PO: 'po_number', Category: 'category', Assignee: 'assignee', Status: 'status', Notes: 'notes', Total: 'total' };
    const ids = [], picked = []; let editing = '';
    for (const cells of bodyRows(tables[0])) {
      const id = txt(cells[1]); ids.push(id);
      if (cells[0].querySelector('input').checked) picked.push(id);
      cells.forEach((c, i) => {
        const f = map[H[i].replace(/[ ▲▼]+$/, '')];
        if (!f) return;
        if (c.querySelector('input,select')) { editing = id + ':' + f; return; }
        out['data/invoice/' + id + '/' + f] = txt(c);
      });
    }
    out['view/invoices/order'] = ids.join(',');
    out['view/editing'] = editing;
    out['view/invoices/sort'] = H.filter((x) => /[▲▼]$/.test(x)).join(',');
    const bars = [...document.querySelectorAll('main > div')];
    const search = document.querySelector('main input[type=text]');
    const selects = [...document.querySelectorAll('main > div select')];
    out['view/invoices/filter'] = JSON.stringify([search ? search.value : '', selects.length ? selects[0].value : '']);
    const pageLabel = bars.map(txt).map((s) => /Page (\d+) of/.exec(s)).filter(Boolean)[0];
    out['view/invoices/page'] = pageLabel ? pageLabel[1] : '';
    const count = bars.map(txt).map((s) => /^(\d+) selected/.exec(s)).filter(Boolean)[0];
    out['view/invoices/selection'] = (count ? count[1] : '0') + ':' + picked.join(',');
    if (count && selects.length >= 3) out['view/invoices/bulk'] = selects[1].value + ':' + selects[2].value;
  } else if (parts[0] === 'invoices' && parts.length === 2) {
    page = 'detail';
    const id = decodeURIComponent(parts[1]);
    const h1 = document.querySelector('main h1');
    const spans = h1 ? [...h1.querySelectorAll('span')] : [];
    if (spans.length === 2) {
      out['data/invoice/' + id + '/status'] = txt(spans[0]);
      out['data/invoice/' + id + '/vendor_id'] = txt(spans[1]);
    }
    const formMap = { 'Vendor': 'vendor_id', 'Invoice date': 'date', 'Due date': 'due_date', 'PO number': 'po_number', 'Category': 'category', 'Assignee': 'assignee', 'Status': 'status', 'Notes': 'notes', 'Discount ($)': 'discount' };
    for (const label of document.querySelectorAll('main label')) {
      const f = formMap[txt(label)];
      const control = label.nextElementSibling;
      if (f && control) out['view/form/' + id + '/' + f] = control.value;
    }
    if (tables.length) {
      const lineIds = []; let editing = '';
      const fields = [null, 'description', 'qty', 'unit_price_cents'];
      for (const cells of bodyRows(tables[0])) {
        const lid = txt(cells[0]); lineIds.push(lid);
        fields.forEach((f, i) => {
          if (!f) return;
          if (cells[i].querySelector('input,select')) { editing = lid + ':' + f; return; }
          out['data/invoice/' + id + '/line/' + lid + '/' + f] = txt(cells[i]);
        });
      }
      out['data/invoice/' + id + '/line-index'] = lineIds.join(',');
      out['view/editing'] = editing;
    }
    const totalMap = { Subtotal: 'subtotal', Discount: 'discount_cents', Tax: 'tax', Total: 'total' };
    for (const row of document.querySelectorAll('main div > div')) {
      const s = row.querySelectorAll(':scope > span');
      if (s.length === 2 && totalMap[txt(s[0])]) out['data/invoice/' + id + '/' + totalMap[txt(s[0])]] = txt(s[1]);
    }
    for (const inp of document.querySelectorAll('main input[placeholder]')) out['view/newline/' + { description: 'description', quantity: 'qty', 'unit price': 'unit_price' }[inp.placeholder]] = inp.value;
  } else if (parts[0] === 'vendors' && tables.length) {
    page = 'vendors';
    const fields = [null, 'name', 'tax_bp', 'early_discount_bp', 'terms_days', 'contact'];
    let editing = '';
    for (const cells of bodyRows(tables[0])) {
      const vid = txt(cells[0]);
      fields.forEach((f, i) => {
        if (!f) return;
        if (cells[i].querySelector('input,select')) { editing = vid + ':' + f; return; }
        out['data/vendor/' + vid + '/' + f] = txt(cells[i]);
      });
    }
    out['view/editing'] = editing;
  } else if (parts[0] === 'payments' && tables.length === 2) {
    page = 'payments';
    const picked = [];
    for (const cells of bodyRows(tables[0])) {
      const id = txt(cells[1]);
      if (cells[0].querySelector('input').checked) picked.push(id);
      out['data/invoice/' + id + '/status'] = 'approved';   // listed here means approved
      out['data/invoice/' + id + '/total'] = txt(cells[3]);
    }
    out['view/payments/selection'] = picked.join(',');
    const bids = [];
    for (const cells of bodyRows(tables[1])) {
      const bid = txt(cells[0]); bids.push(bid);
      out['data/batch/' + bid + '/invoices'] = txt(cells[1]);
      out['data/batch/' + bid + '/total'] = txt(cells[2]);
    }
    out['data/batch-index'] = bids.join(',');
  } else if (parts[0] === 'summary' && tables.length === 2) {
    page = 'summary';
    for (const cells of bodyRows(tables[0])) out['data/summary/category/' + txt(cells[0]) + '/total'] = txt(cells[1]);
    for (const cells of bodyRows(tables[1])) out['data/summary/status/' + txt(cells[0]) + '/count'] = txt(cells[1]);
  }
  return { leaves: out, page };
}
"""

# View leaves whose presence depends only on which controls are drawn.
_PRESENCE_ONLY = ("view/invoices/bulk",)


def _tabbed(path: str, label: str) -> str:
    return f"view/{label}/{path[5:]}" if path.startswith("view/") else path


def model_diff(before: dict[str, Any], after: dict[str, Any], label: str) -> set[str]:
    """Paths whose shown text differs between two page models.

    A data leaf that merely appears or disappears is reported only when the
    page did not navigate and is not the invoice list: on the list, rows come
    and go with paging, sorting and filtering, which is not a data change.
    """
    b, a = before["leaves"], after["leaves"]
    same_page = b["view/route"] == a["view/route"]
    presence_counts = same_page and after["page"] != "invoices"
    out: set[str] = set()
    for path in set(b) | set(a):
        if path in b and path in a:
            if b[path] != a[path]:
                out.add(path)
        elif path.startswith("view/"):
            if path not in _PRESENCE_ONLY:
                out.add(path)
        elif presence_counts:
            out.add(path)
    return {_tabbed(p, label) for p in out}


# ---- mutation_anchor -------------------------------------------------------------
def _anchor_path(chain: list[dict[str, Any]], route: str) -> str | None:  # noqa: C901  one branch per page region
    """Name the labelled region a chain of ``data-*`` ancestors points at.

    ``chain`` runs from the innermost labelled element outward.  Returns None
    when nothing on the chain identifies a region.
    """
    if not chain:
        return None
    first = chain[0]
    if first["view"]:
        return f"view/{first['view']}"
    if first["form"]:
        parts = [p for p in route.split("/") if p]
        return f"view/form/{parts[1]}/{first['form']}" if len(parts) == 2 else None
    field = first["field"]
    entity = next((c for c in chain if c["entity"] and c["id"]), None)
    if entity is None:
        return None
    kind, ident = entity["entity"], entity["id"]
    if kind == "invoice":
        base = f"data/invoice/{ident}"
    elif kind == "vendor":
        base = f"data/vendor/{ident}"
    elif kind == "batch":
        base = f"data/batch/{ident}"
    elif kind == "line":
        owner = next((c for c in chain if c["entity"] == "lines" and c["id"]), None)
        if owner is None:
            return None
        base = f"data/invoice/{owner['id']}/line/{ident}"
    elif kind == "lines":
        return f"data/invoice/{ident}"
    elif kind == "summary-category":
        base = f"data/summary/category/{ident}"
    elif kind == "summary-status":
        base = f"data/summary/status/{ident}"
    else:
        return None
    if field is None or field == "id":
        return base
    return f"{base}/{field}"


def mutation_paths(records: list[dict[str, Any]], route: str, label: str) -> set[str]:
    """Paths named by a list of mutation records.

    A mutation that cannot be tied to any labelled region means "something on
    this page changed": the whole tab view and all data.
    """
    out: set[str] = set()
    for rec in records:
        named = {_anchor_path(c, route) for c in rec["touched"]}
        named.add(_anchor_path(rec["target"], route))
        named.discard(None)
        if not named:
            out.update({"view", "data"})
        else:
            out.update(named)  # type: ignore[arg-type]
    return {f"view/{label}" if p == "view" else _tabbed(p, label) for p in out}


# ---- network -----------------------------------------------------------------------
_INVOICE_LEAVES = (
    "vendor_id",
    "date",
    "due_date",
    "po_number",
    "category",
    "assignee",
    "notes",
    "status",
    "discount_cents",
    "subtotal",
    "tax",
    "total",
)
_LINE_LEAVES = ("description", "qty", "unit_price_cents")
_VENDOR_LEAVES = ("name", "tax_bp", "early_discount_bp", "terms_days", "contact")


class NetworkModel:
    """The app's records as rebuilt from its API replies."""

    def __init__(self) -> None:
        self.invoices: dict[str, dict[str, Any]] = {}
        self.vendors: dict[str, dict[str, Any]] = {}
        self.batches: list[dict[str, Any]] = []
        self.categories: tuple[str, ...] = ()
        self.statuses: tuple[str, ...] = ()
        self.consumed = 0

    def consume(self, entries: list[dict[str, Any]]) -> None:
        for e in entries:
            body = e["response"]
            if e["status"] != 200 or body is None:
                continue
            url = e["url"].split("?")[0]
            if url == "/api/config":
                self.categories = tuple(body["categories"])
                self.statuses = tuple(body["statuses"])
            elif url == "/api/bootstrap":
                self.invoices = {r["id"]: r for r in body["invoices"]}
                self.vendors = {r["id"]: r for r in body["vendors"]}
                self.batches = body["batches"]
            elif url == "/api/changes":
                for r in body["invoices"]:
                    self.invoices[r["id"]] = r
                for iid in body["removed_invoices"]:
                    del self.invoices[iid]
                for r in body["vendors"]:
                    self.vendors[r["id"]] = r
                if body["batches"] is not None:
                    self.batches = body["batches"]
        self.consumed += len(entries)

    def flat(self) -> dict[str, Any]:
        """Leaf paths and values, written independently of the server's own code."""
        if not self.categories:
            raise RuntimeError("network model has not seen /api/config yet")
        out: dict[str, Any] = {"data/invoice-index": ",".join(sorted(self.invoices))}
        cat = {c: 0 for c in self.categories}
        st = {s: 0 for s in self.statuses}
        for iid, r in self.invoices.items():
            for f in _INVOICE_LEAVES:
                out[f"data/invoice/{iid}/{f}"] = r[f]
            out[f"data/invoice/{iid}/line-index"] = ",".join(line["id"] for line in r["lines"])
            for line in r["lines"]:
                for f in _LINE_LEAVES:
                    out[f"data/invoice/{iid}/line/{line['id']}/{f}"] = line[f]
            st[r["status"]] += 1
            if r["status"] != "void":
                cat[r["category"]] += r["total"]
        for vid, r in self.vendors.items():
            for f in _VENDOR_LEAVES:
                out[f"data/vendor/{vid}/{f}"] = r[f]
        out["data/batch-index"] = ",".join(b["id"] for b in self.batches)
        for b in self.batches:
            out[f"data/batch/{b['id']}/invoices"] = ",".join(b["invoices"])
            out[f"data/batch/{b['id']}/total"] = b["total"]
        for c, v in cat.items():
            out[f"data/summary/category/{c}/total"] = v
        for s, v in st.items():
            out[f"data/summary/status/{s}/count"] = v
        return out


def _diff(before: dict[str, Any], after: dict[str, Any]) -> set[str]:
    return {
        k for k in set(before) | set(after) if before.get(k, _MISSING) != after.get(k, _MISSING)
    }


_MISSING = object()


class Observer:
    """Watches one tab and reports the paths changed between ``begin`` and ``end``."""

    def __init__(self, tab: Tab, sources: tuple[str, ...]):
        unknown = [s for s in sources if s not in SOURCES]
        if unknown or not sources:
            raise ValueError(
                f"observer sources must be a non-empty subset of {SOURCES}, got {sources}"
            )
        self.tab = tab
        self.sources = tuple(sources)
        self._net = NetworkModel() if "network" in sources else None
        self._model_before: dict[str, Any] | None = None
        self._net_before: dict[str, Any] | None = None
        self._open = False

    def _pull_network(self) -> None:
        entries = self.tab.page.evaluate("s => window.__sw.netLog.slice(s)", self._net.consumed)
        self._net.consume(entries)

    def begin(self) -> None:
        if self._open:
            raise RuntimeError("observer.begin called twice without end")
        self._open = True
        if "model_diff" in self.sources:
            self._model_before = self.tab.page.evaluate(_MODEL_SCRIPT)
        if "network" in self.sources:
            self._pull_network()
            self._net_before = self._net.flat()
        if "mutation_anchor" in self.sources:
            self.tab.page.evaluate(
                "() => { window.__sw.mutLog = []; window.__sw.recordMutations = true; }"
            )

    def end(self) -> frozenset[str]:
        """Changed paths since ``begin``, reduced to a minimal covering set."""
        if not self._open:
            raise RuntimeError("observer.end called without begin")
        self._open = False
        out: set[str] = set()
        if "model_diff" in self.sources:
            out |= model_diff(
                self._model_before, self.tab.page.evaluate(_MODEL_SCRIPT), self.tab.label
            )
        if "network" in self.sources:
            self._pull_network()
            out |= _diff(self._net_before, self._net.flat())
        if "mutation_anchor" in self.sources:
            raw = self.tab.page.evaluate(
                "() => { window.__sw.recordMutations = false; return { log: window.__sw.mutLog, route: location.pathname }; }"
            )
            out |= mutation_paths(raw["log"], raw["route"], self.tab.label)
        return minimal_cover(out) if out else frozenset()
