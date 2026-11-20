"""JavaScript injected into every page by the harness.

``INIT_SCRIPT`` installs ``window.__sw``: bookkeeping that any browser-level
tool could do (count in-flight requests, notice DOM mutations, log API
traffic, hand out element handles).  It knows nothing about the Ledger app and
never touches the ground-truth instrumentation (``__gt``).
"""

INIT_SCRIPT = r"""
(() => {
  if (window.__sw) return;
  const sw = window.__sw = {
    handles: {}, handleMode: 'ref', inflight: 0, lastActivity: performance.now(),
    netLog: [], mutLog: [], recordMutations: false,
  };
  // ---- network: count in-flight requests and log API traffic ---------------
  const origFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    const method = (init && init.method) || 'GET';
    sw.inflight += 1;
    try {
      const resp = await origFetch(input, init);
      if (url.startsWith('/api/')) {
        let body = null;
        try { body = await resp.clone().json(); } catch (e) { body = null; }
        sw.netLog.push({ method, url, status: resp.status,
                         request: init && init.body ? JSON.parse(init.body) : null, response: body });
      }
      return resp;
    } finally { sw.inflight -= 1; sw.lastActivity = performance.now(); }
  };
  // ---- DOM mutations --------------------------------------------------------
  const chainOf = (node) => {
    const chain = [];
    for (let e = node && node.nodeType === 1 ? node : node && node.parentElement; e; e = e.parentElement) {
      const d = e.dataset;
      if (d && (d.field || d.entity || d.view || d.form)) chain.push({ field: d.field || null, entity: d.entity || null, id: d.id || null, view: d.view || null, form: d.form || null });
    }
    return chain;
  };
  const subtreeChains = (node, out) => {
    if (node.nodeType !== 1) return;
    const els = [node, ...node.querySelectorAll('[data-field],[data-view],[data-form]')];
    for (const el of els) {
      const d = el.dataset;
      if (d.field || d.view || d.form) out.push(chainOf(el));
    }
  };
  const mo = new MutationObserver((records) => {
    sw.lastActivity = performance.now();
    if (!sw.recordMutations) return;
    for (const r of records) {
      const entry = { type: r.type, target: chainOf(r.target), touched: [] };
      if (r.type === 'childList') {
        for (const n of r.addedNodes) subtreeChains(n, entry.touched);
        for (const n of r.removedNodes) {
          // A removed node has lost its ancestors; re-attach the target's chain.
          const sub = []; subtreeChains(n, sub);
          for (const c of sub) entry.touched.push(c.concat(entry.target));
        }
      }
      sw.mutLog.push(entry);
    }
  });
  mo.observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
  // ---- settle: no requests in flight and no activity for `quietMs` -----------
  sw.settle = (quietMs, timeoutMs) => new Promise((resolve) => {
    const start = performance.now();
    const tick = () => {
      const now = performance.now();
      if (sw.inflight === 0 && now - sw.lastActivity >= quietMs) resolve(true);
      else if (now - start >= timeoutMs) resolve(false);
      else setTimeout(tick, 15);
    };
    tick();
  });
  // ---- element handles ---------------------------------------------------------
  // 'ref' keeps the element itself: if the page replaced it, the handle is stale.
  // 'positional' keeps its position in the page: it resolves to whatever is there now.
  const positionOf = (el) => {
    const idx = [];
    for (let e = el; e && e !== document.body; e = e.parentElement) idx.unshift([...e.parentElement.children].indexOf(e));
    return idx;
  };
  sw.register = (id, el) => { sw.handles[id] = sw.handleMode === 'ref' ? { el } : { pos: positionOf(el) }; };
  sw.resolve = (id) => {
    const hd = sw.handles[id];
    if (!hd) return null;
    if (hd.el) return hd.el.isConnected ? hd.el : null;
    let e = document.body;
    for (const i of hd.pos) { e = e.children[i]; if (!e) return null; }
    return e;
  };
})();
"""

# Builds the text view an agent reads.  Generic: uses only tags, visible text,
# labels and form state.  Returns {text, handles: [{id, kind, label}]}.
SNAPSHOT_SCRIPT = r"""
(handleMode) => {
  const sw = window.__sw;
  sw.handles = {}; sw.handleMode = handleMode;
  const out = []; const handles = []; let n = 0;
  const BLOCK = new Set(['DIV','P','MAIN','HEADER','NAV','SECTION','FORM','UL','OL','LI','TABLE','THEAD','TBODY','TR','H1','H2','H3','LABEL']);
  const hidden = (el) => el.getClientRects().length === 0;
  const clean = (s) => String(s).replace(/\s+/g, ' ').trim();
  const kindOf = (el) => {
    const t = el.tagName;
    if (t === 'BUTTON') return 'button';
    if (t === 'A' && el.hasAttribute('href')) return 'link';
    if (t === 'SELECT') return 'select';
    if (t === 'TEXTAREA') return 'input';
    if (t === 'INPUT') return el.type === 'checkbox' ? 'checkbox' : 'input';
    if (el.hasAttribute('tabindex')) return t === 'TH' ? 'header' : t === 'TD' ? 'cell' : 'control';
    return null;
  };
  const marker = (el, kind) => {
    n += 1; const id = 'e' + n; sw.register(id, el);
    const label = clean(el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.innerText || el.getAttribute('title') || '');
    handles.push({ id, kind, label });
    let s = '[' + id + ' ' + kind;
    if (kind === 'cell' || kind === 'header' || kind === 'button' || kind === 'link' || kind === 'control') s += ' ' + JSON.stringify(clean(el.innerText));
    else s += ' ' + JSON.stringify(label);
    if (kind === 'input') s += ' value=' + JSON.stringify(el.value);
    if (kind === 'checkbox') s += el.checked ? ' checked' : ' unchecked';
    if (kind === 'select') {
      const opts = [...el.options].map((o) => clean(o.textContent));
      s += ' value=' + JSON.stringify(el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].textContent) : '') + ' options=' + JSON.stringify(opts);
    }
    if (el.disabled) s += ' disabled';
    return s + ']';
  };
  const inline = (node) => {
    if (node.nodeType === 3) return clean(node.nodeValue);
    if (node.nodeType !== 1 || hidden(node)) return '';
    const kind = kindOf(node);
    if (kind) return marker(node, kind);
    return [...node.childNodes].map(inline).filter(Boolean).join(' ');
  };
  const walk = (el) => {
    if (hidden(el)) return;
    const t = el.tagName;
    if (t === 'SCRIPT' || t === 'STYLE') return;
    if (t === 'H1' || t === 'H2' || t === 'H3') { out.push('#'.repeat(+t[1]) + ' ' + inline(el)); return; }
    if (t === 'TR') { out.push('| ' + [...el.cells].map((c) => (kindOf(c) ? marker(c, kindOf(c)) : inline(c))).join(' | ') + ' |'); return; }
    if (kindOf(el)) { out.push(marker(el, kindOf(el))); return; }
    let buf = [];
    const flush = () => { const s = buf.filter(Boolean).join(' '); if (s) out.push(s); buf = []; };
    for (const c of el.childNodes) {
      if (c.nodeType === 1 && BLOCK.has(c.tagName) && !kindOf(c)) { flush(); walk(c); }
      else buf.push(inline(c));
    }
    flush();
  };
  walk(document.body);
  return { text: 'URL: ' + location.pathname + '\n' + out.join('\n'), handles };
}
"""
