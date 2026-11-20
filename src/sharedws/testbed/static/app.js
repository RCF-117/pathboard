'use strict';
/* Ledger testbed front-end.
 *
 * The page keeps a local copy of the server data and re-draws when it changes.
 * How it re-draws is the experimental variable `render_mode`:
 *   granular       - patch only the text/attributes that differ (keyed rows)
 *   rerender_keyed - rebuild the whole view on every change; elements carry data-* attributes
 *   rerender_anon  - rebuild the whole view on every change; no data-* attributes at all
 *
 * Ground-truth instrumentation lives in `window.__gt` and in `__gt*` element
 * properties.  It is for the experiment harness only: agents and observers
 * must never read it.
 */
(() => {
const $ = (id) => document.getElementById(id);
let CFG = null;
let rendering = false;
const D = { invoices: new Map(), vendors: new Map(), batches: [], seq: 0 };
const S = {
  sort: { col: 'id', dir: 'asc' },
  filter: { search: '', status: '' },
  page: 1,
  selection: new Set(),
  paySelection: new Set(),
  bulk: { field: 'category', value: '' },
  modal: null,     // {kind:'new', draft} | {kind:'delete', id} | {kind:'pay'}
  editing: null,   // {key, entity, id, lid, field, kind, options, draft}
  form: null,      // {iid, base, draft}  (detail page draft; saved only on Save)
  newLine: { description: '', qty: '', unit_price: '' },
  focusKey: null,
  order: [],
};
const FORM_FIELDS = ['vendor_id', 'date', 'due_date', 'po_number', 'category', 'assignee', 'status', 'notes', 'discount'];
const EDITABLE_STATUSES = ['draft', 'review', 'approved', 'void'];

// ---- formatting ------------------------------------------------------------
const money = (c) => '$' + (Math.floor(c / 100)).toLocaleString('en-US') + '.' + String(c % 100).padStart(2, '0');
const plainMoney = (c) => Math.floor(c / 100) + '.' + String(c % 100).padStart(2, '0');
const pct = (bp) => (bp / 100).toFixed(2) + '%';
function parseMoney(s) {
  const t = String(s).replace(/[$,\s]/g, '');
  if (!/^\d+(\.\d{1,2})?$/.test(t)) return null;
  const [a, b = ''] = t.split('.');
  return parseInt(a, 10) * 100 + parseInt((b + '00').slice(0, 2), 10);
}
function parsePct(s) {
  const t = String(s).replace(/[%\s]/g, '');
  if (!/^\d+(\.\d{1,2})?$/.test(t)) return null;
  const [a, b = ''] = t.split('.');
  return parseInt(a, 10) * 100 + parseInt((b + '00').slice(0, 2), 10);
}
function parseCount(s) { const t = String(s).trim(); return /^\d+$/.test(t) ? parseInt(t, 10) : null; }
const invNum = (id) => parseInt(id.slice(4), 10);

// ---- element builder -------------------------------------------------------
const anon = () => CFG.render_mode === 'rerender_anon';
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  const p = props || {};
  for (const [k, v] of Object.entries(p)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'data') { if (!anon()) for (const [dk, dv] of Object.entries(v)) el.dataset[dk] = dv; }
    else if (k === 'gt') { el.__gtKey = v.key; el.__gtPaths = v.paths || []; el.__gtShows = v.shows || []; }
    else if (k === 'on') { for (const [evt, fn] of Object.entries(v)) el.addEventListener(evt, fn); }
    else if (k === 'value') { if (tag !== 'select') el.value = v; }
    else if (k === 'checked') el.checked = true;
    else if (k === 'disabled') el.disabled = true;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  if (tag === 'select' && 'value' in p) el.value = p.value;
  return el;
}
const options = (list, label) => list.map((v) => h('option', { value: v }, label ? label(v) : v));

// ---- granular patching (used only when render_mode === 'granular') ----------
function same(o, n) {
  if (o.nodeType !== n.nodeType) return false;
  return o.nodeType !== 1 || (o.tagName === n.tagName && (o.__gtKey || '') === (n.__gtKey || ''));
}
function morph(o, n) {
  if (o.nodeType === 3) { if (o.nodeValue !== n.nodeValue) o.nodeValue = n.nodeValue; return; }
  for (const a of [...o.attributes]) if (!n.hasAttribute(a.name)) o.removeAttribute(a.name);
  for (const a of [...n.attributes]) if (o.getAttribute(a.name) !== a.value) o.setAttribute(a.name, a.value);
  o.__gtPaths = n.__gtPaths; o.__gtShows = n.__gtShows;
  const oc = [...o.childNodes], nc = [...n.childNodes];
  // Keep the matching head and tail of the child list; replace only the middle.
  let head = 0;
  while (head < oc.length && head < nc.length && same(oc[head], nc[head])) head += 1;
  let tail = 0;
  while (tail < oc.length - head && tail < nc.length - head && same(oc[oc.length - 1 - tail], nc[nc.length - 1 - tail])) tail += 1;
  for (let i = 0; i < head; i += 1) morph(oc[i], nc[i]);
  for (let i = 1; i <= tail; i += 1) morph(oc[oc.length - i], nc[nc.length - i]);
  for (let i = head; i < oc.length - tail; i += 1) oc[i].remove();
  const before = tail > 0 ? oc[oc.length - tail] : null;
  for (let i = head; i < nc.length - tail; i += 1) o.insertBefore(nc[i], before);
  if (o.tagName === 'INPUT' && o.type === 'checkbox') { if (o.checked !== n.checked) o.checked = n.checked; }
  else if (o.tagName === 'INPUT' || o.tagName === 'SELECT') {
    if (o.value !== n.value && document.activeElement !== o) o.value = n.value;
  }
}

// ---- server access ---------------------------------------------------------
let toastTimer = null;
function toast(msg) {
  $('toast').textContent = msg;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('toast').textContent = ''; }, 6000);
}
async function api(method, path, body) {
  const r = await fetch(path, {
    method, headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const j = await r.json();
  if (!r.ok) { toast('Error: ' + j.error); return null; }
  return j;
}
let syncChain = Promise.resolve();
function sync(force) { syncChain = syncChain.then(() => doSync(force)); return syncChain; }
async function doSync(force) {
  const r = await fetch('/api/changes?since=' + D.seq);
  const c = await r.json();
  let changed = false;
  for (const inv of c.invoices) { D.invoices.set(inv.id, inv); changed = true; }
  for (const id of c.removed_invoices) { D.invoices.delete(id); S.selection.delete(id); changed = true; }
  for (const v of c.vendors) { D.vendors.set(v.id, v); changed = true; }
  if (c.batches) { D.batches = c.batches; changed = true; }
  D.seq = c.seq;
  for (const id of [...S.paySelection]) {
    const inv = D.invoices.get(id);
    if (!inv || inv.status !== 'approved') S.paySelection.delete(id);
  }
  if (changed || force) render();
}
async function mutate(method, path, body) {
  const res = await api(method, path, body);
  await sync(true);
  return res;
}

// ---- routing ---------------------------------------------------------------
function route() {
  const parts = location.pathname.split('/').filter(Boolean);
  if (parts[0] === 'invoices' && parts[1]) return { name: 'detail', id: decodeURIComponent(parts[1]) };
  if (['invoices', 'vendors', 'payments', 'summary'].includes(parts[0])) return { name: parts[0] };
  return { name: 'invoices' };
}
function formFrom(rec) {
  const f = {};
  for (const k of FORM_FIELDS) f[k] = k === 'discount' ? plainMoney(rec.discount_cents) : rec[k];
  return f;
}
function onRoute() {
  if (location.pathname === '/') history.replaceState(null, '', '/invoices');
  const r = route();
  S.editing = null; S.modal = null; S.focusKey = null;
  S.newLine = { description: '', qty: '', unit_price: '' };
  S.form = null;
  if (r.name === 'detail' && D.invoices.has(r.id)) {
    const f = formFrom(D.invoices.get(r.id));
    S.form = { iid: r.id, base: { ...f }, draft: { ...f } };
  }
  render();
}
function navigate(path) { history.pushState(null, '', path); onRoute(); }
window.addEventListener('popstate', onRoute);

// ---- inline editing --------------------------------------------------------
// Enter commits, Escape cancels, starting another edit discards this one.
function startEdit(desc, current) {
  S.editing = { ...desc, draft: current };
  S.focusKey = 'editor';
  render();
}
function cancelEdit() { S.editing = null; S.focusKey = null; render(); }
async function commitEdit(raw) {
  const e = S.editing;
  S.editing = null; S.focusKey = null;
  let value = raw;
  if (e.parse) {
    value = e.parse(raw);
    if (value === null) { toast('Error: "' + raw + '" is not a valid value for ' + e.label); render(); return; }
  }
  const body = { [e.field]: value };
  if (e.entity === 'invoice') await mutate('PATCH', '/api/invoices/' + e.id, body);
  else if (e.entity === 'line') await mutate('PATCH', '/api/invoices/' + e.id + '/lines/' + e.lid, body);
  else if (e.entity === 'vendor') await mutate('PATCH', '/api/vendors/' + e.id, body);
}
function editor(e, paths) {
  const gt = { key: 'editor', paths };
  const onKey = (ev) => {
    if (ev.key === 'Escape') { ev.preventDefault(); cancelEdit(); }
    else if (ev.key === 'Enter' && e.kind === 'text') { ev.preventDefault(); commitEdit(ev.target.value); }
  };
  if (e.kind === 'select') {
    return h('select', { gt, 'aria-label': 'Edit ' + e.label, value: e.draft,
      on: { change: (ev) => commitEdit(ev.target.value), keydown: onKey, click: (ev) => ev.stopPropagation() } },
      options(e.options, e.optionLabel));
  }
  return h('input', { gt, type: 'text', 'aria-label': 'Edit ' + e.label, value: e.draft,
    on: { input: (ev) => { if (S.editing) S.editing.draft = ev.target.value; }, keydown: onKey,
          click: (ev) => ev.stopPropagation() } });
}
/* A table cell.  `edit` (optional) describes how it is edited in place. */
function cell(key, paths, text, opts) {
  const o = opts || {};
  const cls = (o.num ? 'num ' : '') + (o.locked ? 'locked' : '');
  const data = o.data || {};
  if (o.edit && S.editing && S.editing.key === key) {
    return h('td', { class: cls, data, gt: { key, paths, shows: paths } }, editor(S.editing, paths));
  }
  if (!o.edit) return h('td', { class: cls || null, data, gt: { key, paths, shows: paths } }, text);
  const begin = () => startEdit({ key, ...o.edit }, o.edit.current !== undefined ? o.edit.current : text);
  return h('td', { class: cls || null, tabindex: 0, data, title: 'Click to edit', gt: { key, paths, shows: paths },
    on: { click: begin, keydown: (ev) => { if (ev.key === 'Enter' && ev.target === ev.currentTarget) { ev.preventDefault(); begin(); } } } }, text);
}

// ---- invoices list ---------------------------------------------------------
function visibleInvoices() {
  const q = S.filter.search.trim().toLowerCase();
  const rows = [...D.invoices.values()].filter((r) =>
    (!S.filter.status || r.status === S.filter.status) &&
    (!q || (r.id + ' ' + r.vendor_name + ' ' + r.po_number).toLowerCase().includes(q)));
  const col = S.sort.col, sgn = S.sort.dir === 'asc' ? 1 : -1;
  const key = (r) => (col === 'id' ? invNum(r.id) : col === 'vendor' ? r.vendor_name : r[col]);
  rows.sort((a, b) => {
    const x = key(a), y = key(b);
    if (x < y) return -sgn;
    if (x > y) return sgn;
    return invNum(a.id) - invNum(b.id);
  });
  const pages = Math.max(1, Math.ceil(rows.length / CFG.page_size));
  if (S.page > pages) S.page = pages;
  return { rows: rows.slice((S.page - 1) * CFG.page_size, S.page * CFG.page_size), pages, total: rows.length };
}
function bulkOptions(field) {
  return field === 'category' ? CFG.categories : field === 'assignee' ? CFG.assignees : EDITABLE_STATUSES;
}
function invoicesPage() {
  const cols = [
    { id: 'id', label: 'Invoice', sort: true }, { id: 'vendor', label: 'Vendor', sort: true },
    { id: 'date', label: 'Date', sort: true }, { id: 'po_number', label: 'PO' },
    { id: 'category', label: 'Category', sort: true }, { id: 'assignee', label: 'Assignee', sort: true },
    { id: 'status', label: 'Status', sort: true }, { id: 'notes', label: 'Notes' },
    { id: 'total', label: 'Total', sort: true, num: true },
  ];
  const { rows, pages, total } = visibleInvoices();
  S.order = rows.map((r) => r.id);
  const setSort = (c) => {
    S.sort = { col: c, dir: S.sort.col === c && S.sort.dir === 'asc' ? 'desc' : 'asc' };
    S.page = 1; S.editing = null; render();
  };
  const head = h('tr', null,
    h('th', null, h('input', { type: 'checkbox', 'aria-label': 'Select all on this page',
      checked: rows.length > 0 && rows.every((r) => S.selection.has(r.id)),
      gt: { key: 'row-check:all', paths: ['view/invoices/selection'] },
      on: { change: (ev) => { for (const r of rows) ev.target.checked ? S.selection.add(r.id) : S.selection.delete(r.id); render(); } } })),
    cols.map((c) => {
      const arrow = S.sort.col === c.id ? (S.sort.dir === 'asc' ? ' ▲' : ' ▼') : '';
      if (!c.sort) return h('th', { class: c.num ? 'num' : null }, c.label);
      return h('th', { class: c.num ? 'num' : null, tabindex: 0, title: 'Sort by ' + c.label, data: { view: 'invoices/sort' },
        gt: { key: 'hdr:' + c.id, paths: ['view/invoices/sort'] },
        on: { click: () => setSort(c.id), keydown: (ev) => { if (ev.key === 'Enter') setSort(c.id); } } }, c.label + arrow);
    }),
    h('th', null, ''));
  const body = rows.map((r) => {
    const base = 'data/invoice/' + r.id;
    const locked = r.status === 'paid' || r.status === 'void';
    const k = (f) => 'cell:invoice:' + r.id + ':' + f;
    const d = (f) => ({ field: f });
    const ed = (field, kind, label, extra) => (locked ? null : { entity: 'invoice', id: r.id, field, kind, label, ...extra });
    return h('tr', { data: { entity: 'invoice', id: r.id }, gt: { key: 'row:invoice:' + r.id, paths: [base] } },
      h('td', null, h('input', { type: 'checkbox', 'aria-label': 'Select ' + r.id, checked: S.selection.has(r.id),
        gt: { key: 'row-check:' + r.id, paths: [base] },
        on: { change: (ev) => { ev.target.checked ? S.selection.add(r.id) : S.selection.delete(r.id); render(); } } })),
      h('td', { data: d('id') }, h('a', { href: '/invoices/' + r.id, gt: { key: 'link:' + r.id, paths: [base] },
        on: { click: (ev) => { ev.preventDefault(); navigate('/invoices/' + r.id); } } }, r.id)),
      cell(k('vendor_id'), [base + '/vendor_id', 'data/vendor/' + r.vendor_id + '/name'], r.vendor_name, { data: d('vendor_id') }),
      cell(k('date'), [base + '/date'], r.date, { data: d('date') }),
      cell(k('po_number'), [base + '/po_number'], r.po_number, { data: d('po_number') }),
      cell(k('category'), [base + '/category'], r.category, { data: d('category'), locked, edit: ed('category', 'select', 'category of ' + r.id, { options: CFG.categories }) }),
      cell(k('assignee'), [base + '/assignee'], r.assignee, { data: d('assignee'), locked, edit: ed('assignee', 'select', 'assignee of ' + r.id, { options: CFG.assignees }) }),
      cell(k('status'), [base + '/status'], r.status, { data: d('status'), locked, edit: ed('status', 'select', 'status of ' + r.id, { options: EDITABLE_STATUSES }) }),
      cell(k('notes'), [base + '/notes'], r.notes, { data: d('notes'), locked, edit: ed('notes', 'text', 'notes of ' + r.id) }),
      cell(k('total'), [base + '/total'], money(r.total), { data: d('total'), num: true }),
      h('td', null, r.status === 'draft' ? h('button', { class: 'danger', 'aria-label': 'Delete ' + r.id,
        gt: { key: 'del:' + r.id, paths: [base] }, on: { click: () => { S.modal = { kind: 'delete', id: r.id }; render(); } } }, 'Delete') : ''));
  });
  const bulk = S.selection.size === 0 ? null : h('div', { class: 'bar', data: { view: 'invoices/selection' } },
    h('strong', null, S.selection.size + ' selected'),
    h('span', null, 'Set'),
    h('select', { 'aria-label': 'Bulk field', value: S.bulk.field, gt: { key: 'bulk:field', paths: ['view/invoices/bulk'] },
      on: { change: (ev) => { S.bulk = { field: ev.target.value, value: bulkOptions(ev.target.value)[0] }; render(); } } },
      options(['category', 'assignee', 'status'])),
    h('span', null, 'to'),
    h('select', { 'aria-label': 'Bulk value', value: S.bulk.value || bulkOptions(S.bulk.field)[0],
      gt: { key: 'bulk:value', paths: ['view/invoices/bulk'] },
      on: { change: (ev) => { S.bulk.value = ev.target.value; recordView(); } } }, options(bulkOptions(S.bulk.field))),
    h('button', { class: 'primary', gt: { key: 'bulk:apply', paths: [...S.selection].sort().map((i) => 'data/invoice/' + i) },
      on: { click: async () => {
        const ids = [...S.selection].sort((a, b) => invNum(a) - invNum(b));
        const value = S.bulk.value || bulkOptions(S.bulk.field)[0];
        const res = await api('POST', '/api/bulk', { ids, fields: { [S.bulk.field]: value } });
        if (res) S.selection.clear();
        await sync(true);
      } } }, 'Apply to selected'),
    h('button', { gt: { key: 'bulk:clear', paths: ['view/invoices/selection'] },
      on: { click: () => { S.selection.clear(); render(); } } }, 'Clear selection'));
  return h('main', { id: 'main' },
    h('h1', null, 'Invoices'),
    h('div', { class: 'bar' },
      h('input', { type: 'text', placeholder: 'Search invoice, vendor or PO', 'aria-label': 'Search', value: S.filter.search,
        data: { view: 'invoices/filter' }, gt: { key: 'ctl:search', paths: ['view/invoices/filter'] },
        on: { input: (ev) => { S.filter.search = ev.target.value; S.page = 1; S.focusKey = 'ctl:search'; render(); } } }),
      h('select', { 'aria-label': 'Status filter', value: S.filter.status, data: { view: 'invoices/filter' },
        gt: { key: 'ctl:status-filter', paths: ['view/invoices/filter'] },
        on: { change: (ev) => { S.filter.status = ev.target.value; S.page = 1; render(); } } },
        options(['', ...CFG.statuses], (v) => v || 'All statuses')),
      h('span', { class: 'grow' }),
      h('button', { class: 'primary', gt: { key: 'ctl:new', paths: ['view/modal'] },
        on: { click: () => { S.modal = { kind: 'new', draft: { vendor_id: [...D.vendors.keys()][0], date: '', category: CFG.categories[0], assignee: CFG.assignees[0] } }; render(); } } }, 'New invoice')),
    bulk,
    h('table', { data: { view: 'invoices/order' } }, h('thead', null, head), h('tbody', { data: { view: 'invoices/order' } }, body)),
    h('div', { class: 'bar', style: 'margin-top:10px', data: { view: 'invoices/page' } },
      h('button', { disabled: S.page <= 1, gt: { key: 'ctl:prev', paths: ['view/invoices/page'] },
        on: { click: () => { S.page -= 1; S.editing = null; render(); } } }, 'Previous page'),
      h('span', null, 'Page ' + S.page + ' of ' + pages + ' (' + total + ' invoices)'),
      h('button', { disabled: S.page >= pages, gt: { key: 'ctl:next', paths: ['view/invoices/page'] },
        on: { click: () => { S.page += 1; S.editing = null; render(); } } }, 'Next page')));
}

// ---- invoice detail --------------------------------------------------------
function detailPage(id) {
  const r = D.invoices.get(id);
  const back = h('a', { href: '/invoices', gt: { key: 'back', paths: ['view/route'] },
    on: { click: (ev) => { ev.preventDefault(); navigate('/invoices'); } } }, 'Back to invoices');
  if (!r || !S.form) return h('main', { id: 'main' }, h('h1', null, 'Invoice ' + id + ' not found'), back);
  const base = 'data/invoice/' + id;
  const locked = r.status === 'paid' || r.status === 'void';
  const f = S.form.draft;
  const set = (k) => (ev) => { S.form.draft[k] = ev.target.value; recordView(); };
  const field = (k, label, control) => [h('label', null, label), control];
  const text = (k, label) => field(k, label, h('input', { type: 'text', 'aria-label': label, value: f[k], disabled: locked,
    data: { form: k }, gt: { key: 'form:' + k, paths: ['view/form/' + id + '/' + k] }, on: { input: set(k) } }));
  const sel = (k, label, list, lab) => field(k, label, h('select', { 'aria-label': label, value: f[k], disabled: locked,
    data: { form: k }, gt: { key: 'form:' + k, paths: ['view/form/' + id + '/' + k] }, on: { change: set(k) } }, options(list, lab)));
  const statusList = EDITABLE_STATUSES.includes(r.status) ? EDITABLE_STATUSES : [r.status, ...EDITABLE_STATUSES];
  const save = async () => {
    const body = {};
    for (const k of FORM_FIELDS) {
      if (S.form.draft[k] === S.form.base[k]) continue;
      if (k === 'discount') {
        const c = parseMoney(S.form.draft[k]);
        if (c === null) { toast('Error: "' + S.form.draft[k] + '" is not a valid discount'); return; }
        body.discount_cents = c;
      } else body[k] = S.form.draft[k];
    }
    if (Object.keys(body).length === 0) { toast('Nothing to save'); return; }
    const res = await api('PATCH', '/api/invoices/' + id, body);
    if (res) S.form.base = { ...S.form.draft };
    await sync(true);
  };
  const lineRows = r.lines.map((l) => {
    const lb = base + '/line/' + l.id;
    const k = (fl) => 'line:' + l.id + ':' + fl;
    const ed = (fieldName, label, extra) => (locked ? null : { entity: 'line', id, lid: l.id, field: fieldName, kind: 'text', label: label + ' of line ' + l.id, ...extra });
    return h('tr', { data: { entity: 'line', id: l.id }, gt: { key: 'row:line:' + l.id, paths: [lb] } },
      h('td', { data: { field: 'id' } }, l.id),
      cell(k('description'), [lb + '/description'], l.description, { data: { field: 'description' }, locked, edit: ed('description', 'description') }),
      cell(k('qty'), [lb + '/qty'], String(l.qty), { data: { field: 'qty' }, num: true, locked, edit: ed('qty', 'quantity', { parse: parseCount }) }),
      cell(k('unit_price_cents'), [lb + '/unit_price_cents'], money(l.unit_price_cents), { data: { field: 'unit_price_cents' }, num: true, locked,
        edit: ed('unit_price_cents', 'unit price', { parse: parseMoney, current: plainMoney(l.unit_price_cents) }) }),
      h('td', { class: 'num' }, money(l.qty * l.unit_price_cents)),
      h('td', null, locked ? '' : h('button', { class: 'danger', 'aria-label': 'Remove line ' + l.id, gt: { key: 'line-del:' + l.id, paths: [lb] },
        on: { click: () => mutate('DELETE', '/api/invoices/' + id + '/lines/' + l.id) } }, 'Remove')));
  });
  const nl = (k, label) => h('input', { type: 'text', placeholder: label, 'aria-label': 'New line ' + label, value: S.newLine[k],
    gt: { key: 'newline:' + k, paths: ['view/newline/' + k] }, on: { input: (ev) => { S.newLine[k] = ev.target.value; recordView(); } } });
  const addLine = async () => {
    const qty = parseCount(S.newLine.qty), price = parseMoney(S.newLine.unit_price);
    if (qty === null || price === null) { toast('Error: new line needs a whole-number quantity and a price'); return; }
    const res = await api('POST', '/api/invoices/' + id + '/lines', { description: S.newLine.description, qty, unit_price_cents: price });
    if (res) S.newLine = { description: '', qty: '', unit_price: '' };
    await sync(true);
  };
  const tot = (label, key, val, cls) => h('div', { class: cls || null }, h('span', null, label),
    h('span', { data: { field: key }, gt: { key: 'total:' + key, paths: [base + '/' + key], shows: [base + '/' + key] } }, val));
  return h('main', { id: 'main' },
    h('div', { class: 'bar' }, back),
    h('h1', { data: { entity: 'invoice', id } }, 'Invoice ' + id + ' ',
      h('span', { class: 'badge', data: { field: 'status' }, gt: { key: 'saved:status', paths: [base + '/status'], shows: [base + '/status'] } }, r.status), ' ',
      h('span', { class: 'muted', data: { field: 'vendor_id' }, gt: { key: 'saved:vendor', paths: [base + '/vendor_id'], shows: [base + '/vendor_id', 'data/vendor/' + r.vendor_id + '/name'] } }, r.vendor_name)),
    h('div', { class: 'form' },
      sel('vendor_id', 'Vendor', [...D.vendors.keys()], (v) => D.vendors.get(v).name),
      text('date', 'Invoice date'), text('due_date', 'Due date'), text('po_number', 'PO number'),
      sel('category', 'Category', CFG.categories), sel('assignee', 'Assignee', CFG.assignees),
      sel('status', 'Status', statusList), text('notes', 'Notes'), text('discount', 'Discount ($)')),
    h('div', { class: 'bar', style: 'margin-top:10px' },
      h('button', { class: 'primary', disabled: locked, gt: { key: 'form:save', paths: [base] }, on: { click: save } }, 'Save changes'),
      h('button', { disabled: locked, gt: { key: 'form:cancel', paths: ['view/form/' + id] },
        on: { click: () => { const d = formFrom(D.invoices.get(id)); S.form = { iid: id, base: { ...d }, draft: { ...d } }; render(); } } }, 'Discard changes')),
    h('h2', null, 'Line items'),
    h('table', { data: { entity: 'lines', id } },
      h('thead', null, h('tr', null, h('th', null, 'Line'), h('th', null, 'Description'), h('th', { class: 'num' }, 'Qty'),
        h('th', { class: 'num' }, 'Unit price'), h('th', { class: 'num' }, 'Amount'), h('th', null, ''))),
      h('tbody', { data: { entity: 'lines', id } }, lineRows)),
    locked ? null : h('div', { class: 'bar', style: 'margin-top:8px' },
      nl('description', 'description'), nl('qty', 'quantity'), nl('unit_price', 'unit price'),
      h('button', { gt: { key: 'newline:add', paths: [base + '/line-index'] }, on: { click: addLine } }, 'Add line')),
    h('div', { class: 'totals' },
      tot('Subtotal', 'subtotal', money(r.subtotal)), tot('Discount', 'discount_cents', money(r.discount_cents)),
      tot('Tax', 'tax', money(r.tax)), tot('Total', 'total', money(r.total), 'grand')));
}

// ---- vendors ---------------------------------------------------------------
function vendorsPage() {
  const rows = [...D.vendors.values()].map((v) => {
    const base = 'data/vendor/' + v.id;
    const k = (f) => 'cell:vendor:' + v.id + ':' + f;
    const ed = (field, kind, label, extra) => ({ entity: 'vendor', id: v.id, field, kind, label: label + ' of ' + v.id, ...extra });
    return h('tr', { data: { entity: 'vendor', id: v.id }, gt: { key: 'row:vendor:' + v.id, paths: [base] } },
      h('td', { data: { field: 'id' } }, v.id),
      cell(k('name'), [base + '/name'], v.name, { data: { field: 'name' }, edit: ed('name', 'text', 'name') }),
      cell(k('tax_bp'), [base + '/tax_bp'], pct(v.tax_bp), { data: { field: 'tax_bp' }, num: true, edit: ed('tax_bp', 'text', 'tax rate', { parse: parsePct, current: (v.tax_bp / 100).toFixed(2) }) }),
      cell(k('early_discount_bp'), [base + '/early_discount_bp'], pct(v.early_discount_bp), { data: { field: 'early_discount_bp' }, num: true,
        edit: ed('early_discount_bp', 'text', 'early-payment discount', { parse: parsePct, current: (v.early_discount_bp / 100).toFixed(2) }) }),
      cell(k('terms_days'), [base + '/terms_days'], String(v.terms_days), { data: { field: 'terms_days' }, num: true, edit: ed('terms_days', 'text', 'payment terms', { parse: parseCount }) }),
      cell(k('contact'), [base + '/contact'], v.contact, { data: { field: 'contact' }, edit: ed('contact', 'select', 'contact', { options: CFG.assignees }) }));
  });
  return h('main', { id: 'main' }, h('h1', null, 'Vendors'),
    h('table', { data: { entity: 'vendors' } },
      h('thead', null, h('tr', null, h('th', null, 'Vendor ID'), h('th', null, 'Name'), h('th', { class: 'num' }, 'Tax rate'),
        h('th', { class: 'num' }, 'Early-payment discount'), h('th', { class: 'num' }, 'Terms (days)'), h('th', null, 'Contact'))),
      h('tbody', { data: { entity: 'vendors' } }, rows)));
}

// ---- payments --------------------------------------------------------------
function paymentsPage() {
  const approved = [...D.invoices.values()].filter((r) => r.status === 'approved').sort((a, b) => invNum(a.id) - invNum(b.id));
  const rows = approved.map((r) => {
    const base = 'data/invoice/' + r.id;
    return h('tr', { data: { entity: 'invoice', id: r.id }, gt: { key: 'row:pay:' + r.id, paths: [base] } },
      h('td', null, h('input', { type: 'checkbox', 'aria-label': 'Pay ' + r.id, checked: S.paySelection.has(r.id),
        gt: { key: 'pay-check:' + r.id, paths: [base] },
        on: { change: (ev) => { ev.target.checked ? S.paySelection.add(r.id) : S.paySelection.delete(r.id); render(); } } })),
      h('td', { data: { field: 'id' }, gt: { key: 'pay:id:' + r.id, paths: [base + '/status'], shows: [base + '/status'] } }, r.id),
      h('td', { data: { field: 'vendor_id' } }, r.vendor_name),
      h('td', { class: 'num', data: { field: 'total' }, gt: { key: 'pay:total:' + r.id, paths: [base + '/total'], shows: [base + '/total'] } }, money(r.total)));
  });
  const batches = D.batches.map((b) => h('tr', { data: { entity: 'batch', id: b.id }, gt: { key: 'row:batch:' + b.id, paths: ['data/batch/' + b.id] } },
    h('td', { data: { field: 'id' } }, b.id),
    h('td', { data: { field: 'invoices' }, gt: { key: 'batch:inv:' + b.id, paths: [], shows: ['data/batch/' + b.id + '/invoices'] } }, b.invoices.join(', ')),
    h('td', { class: 'num', data: { field: 'total' }, gt: { key: 'batch:total:' + b.id, paths: [], shows: ['data/batch/' + b.id + '/total'] } }, money(b.total))));
  return h('main', { id: 'main' }, h('h1', null, 'Payments'),
    h('h2', null, 'Approved invoices awaiting payment'),
    h('table', { data: { entity: 'payable' } },
      h('thead', null, h('tr', null, h('th', null, ''), h('th', null, 'Invoice'), h('th', null, 'Vendor'), h('th', { class: 'num' }, 'Total'))),
      h('tbody', { data: { entity: 'payable' } }, rows)),
    h('div', { class: 'bar', style: 'margin-top:10px' },
      h('button', { class: 'primary', disabled: S.paySelection.size === 0, gt: { key: 'pay:create', paths: ['view/modal'] },
        on: { click: () => { S.modal = { kind: 'pay' }; render(); } } }, 'Create payment batch'),
      h('span', { class: 'muted' }, S.paySelection.size + ' selected')),
    h('h2', null, 'Payment batches'),
    h('table', { data: { entity: 'batches' } },
      h('thead', null, h('tr', null, h('th', null, 'Batch'), h('th', null, 'Invoices'), h('th', { class: 'num' }, 'Total'))),
      h('tbody', { data: { entity: 'batches' }, gt: { key: 'batches', paths: [], shows: ['data/batch-index'] } }, batches)));
}

// ---- summary ---------------------------------------------------------------
function summaryPage() {
  const cat = Object.fromEntries(CFG.categories.map((c) => [c, 0]));
  const st = Object.fromEntries(CFG.statuses.map((s) => [s, 0]));
  for (const r of D.invoices.values()) { st[r.status] += 1; if (r.status !== 'void') cat[r.category] += r.total; }
  return h('main', { id: 'main' }, h('h1', null, 'Summary'),
    h('h2', null, 'Total by category (void invoices excluded)'),
    h('table', { data: { entity: 'summary-category' } }, h('thead', null, h('tr', null, h('th', null, 'Category'), h('th', { class: 'num' }, 'Total'))),
      h('tbody', null, CFG.categories.map((c) => h('tr', { data: { entity: 'summary-category', id: c } }, h('td', null, c),
        h('td', { class: 'num', data: { field: 'total' }, gt: { key: 'sum:cat:' + c, paths: [], shows: ['data/summary/category/' + c + '/total'] } }, money(cat[c])))))),
    h('h2', null, 'Invoices by status'),
    h('table', { data: { entity: 'summary-status' } }, h('thead', null, h('tr', null, h('th', null, 'Status'), h('th', { class: 'num' }, 'Count'))),
      h('tbody', null, CFG.statuses.map((s) => h('tr', { data: { entity: 'summary-status', id: s } }, h('td', null, s),
        h('td', { class: 'num', data: { field: 'count' }, gt: { key: 'sum:status:' + s, paths: [], shows: ['data/summary/status/' + s + '/count'] } }, String(st[s])))))));
}

// ---- modals ----------------------------------------------------------------
function overlay() {
  const box = h('div', { id: 'overlay' });
  const m = S.modal;
  if (!m) return box;
  const close = () => { S.modal = null; render(); };
  const cancel = h('button', { gt: { key: 'modal:cancel', paths: ['view/modal'] }, on: { click: close } }, 'Cancel');
  if (m.kind === 'delete') {
    box.append(h('div', { class: 'modal', role: 'dialog', data: { view: 'modal' } }, h('h2', null, 'Delete ' + m.id + '?'),
      h('p', null, 'The invoice and its line items will be removed.'),
      h('div', { class: 'actions' }, cancel, h('button', { class: 'primary', gt: { key: 'modal:confirm', paths: ['data/invoice/' + m.id] },
        on: { click: async () => { S.modal = null; await mutate('DELETE', '/api/invoices/' + m.id); } } }, 'Delete invoice'))));
  } else if (m.kind === 'pay') {
    const ids = [...S.paySelection].sort((a, b) => invNum(a) - invNum(b));
    const total = ids.reduce((s, i) => s + D.invoices.get(i).total, 0);
    box.append(h('div', { class: 'modal', role: 'dialog', data: { view: 'modal' } }, h('h2', null, 'Create payment batch?'),
      h('p', null, 'Pay ' + ids.length + ' invoice(s) totalling ' + money(total) + ': ' + ids.join(', ') + '. This cannot be undone.'),
      h('div', { class: 'actions' }, cancel, h('button', { class: 'primary', gt: { key: 'modal:confirm', paths: ids.map((i) => 'data/invoice/' + i) },
        on: { click: async () => { S.modal = null; const res = await api('POST', '/api/batches', { ids }); if (res) S.paySelection.clear(); await sync(true); } } }, 'Confirm payment'))));
  } else if (m.kind === 'new') {
    const set = (k) => (ev) => { m.draft[k] = ev.target.value; recordView(); };
    const sel = (k, label, list, lab) => [h('label', null, label), h('select', { 'aria-label': 'New invoice ' + label, value: m.draft[k],
      gt: { key: 'new:' + k, paths: ['view/newinvoice/' + k] }, on: { change: set(k) } }, options(list, lab))];
    box.append(h('div', { class: 'modal', role: 'dialog', data: { view: 'modal' } }, h('h2', null, 'New invoice'),
      h('div', { class: 'form' },
        sel('vendor_id', 'vendor', [...D.vendors.keys()], (v) => D.vendors.get(v).name),
        h('label', null, 'date'), h('input', { type: 'text', placeholder: 'YYYY-MM-DD', 'aria-label': 'New invoice date', value: m.draft.date,
          gt: { key: 'new:date', paths: ['view/newinvoice/date'] }, on: { input: set('date') } }),
        sel('category', 'category', CFG.categories), sel('assignee', 'assignee', CFG.assignees)),
      h('div', { class: 'actions' }, cancel, h('button', { class: 'primary', gt: { key: 'new:create', paths: ['data/invoice-index'] },
        on: { click: async () => {
          if (!/^\d{4}-\d{2}-\d{2}$/.test(m.draft.date)) { toast('Error: date must look like YYYY-MM-DD'); return; }
          const res = await api('POST', '/api/invoices', m.draft);
          if (res) S.modal = null;
          await sync(true);
        } } }, 'Create invoice'))));
  }
  return box;
}

// ---- drawing ---------------------------------------------------------------
function nav() {
  const r = route();
  const active = r.name === 'detail' ? 'invoices' : r.name;
  return h('nav', { id: 'nav' }, ['invoices', 'vendors', 'payments', 'summary'].map((n) =>
    h('a', { href: '/' + n, class: n === active ? 'active' : null, gt: { key: 'nav:' + n, paths: ['view/route'] },
      on: { click: (ev) => { ev.preventDefault(); navigate('/' + n); } } }, n[0].toUpperCase() + n.slice(1))));
}
function place(live, fresh) {
  if (CFG.render_mode === 'granular') morph(live, fresh);
  else live.replaceChildren(...fresh.childNodes);
}
function render() {
  rendering = true;
  const r = route();
  const main = r.name === 'detail' ? detailPage(r.id) : r.name === 'vendors' ? vendorsPage()
    : r.name === 'payments' ? paymentsPage() : r.name === 'summary' ? summaryPage() : invoicesPage();
  place($('nav'), nav());
  place($('main'), main);
  place($('overlay'), overlay());
  if (S.focusKey) {
    const el = window.__gt.find(S.focusKey);
    if (el && document.activeElement !== el) {
      el.focus();
      if (el.tagName === 'INPUT' && el.type === 'text') el.setSelectionRange(el.value.length, el.value.length);
    }
  }
  rendering = false;
  recordView();
}

// ---- ground truth (experiment harness only) ---------------------------------
function viewLeaves() {
  const v = {
    route: location.pathname,
    modal: S.modal ? S.modal.kind + (S.modal.id ? ':' + S.modal.id : '') : '',
    editing: S.editing ? S.editing.key : '',
    'invoices/sort': S.sort.col + ':' + S.sort.dir,
    'invoices/filter': JSON.stringify([S.filter.search, S.filter.status]),
    'invoices/page': String(S.page),
    'invoices/selection': [...S.selection].sort().join(','),
    'invoices/bulk': S.bulk.field + ':' + S.bulk.value,
    'payments/selection': [...S.paySelection].sort().join(','),
  };
  const r = route();
  if (r.name === 'invoices') v['invoices/order'] = S.order.join(',');
  if (r.name === 'detail') for (const k of Object.keys(S.newLine)) v['newline/' + k] = S.newLine[k];
  if (S.form) for (const k of FORM_FIELDS) v['form/' + S.form.iid + '/' + k] = String(S.form.draft[k]);
  if (S.modal && S.modal.kind === 'new') for (const k of Object.keys(S.modal.draft)) v['newinvoice/' + k] = S.modal.draft[k];
  return v;
}
let lastView = {};
function recordView() {
  const now = viewLeaves();
  for (const k of new Set([...Object.keys(lastView), ...Object.keys(now)])) {
    if (lastView[k] !== now[k]) {
      window.__gt.viewLog.push({ t: performance.now(), leaf: k, old: lastView[k] === undefined ? null : lastView[k], new: now[k] === undefined ? null : now[k] });
    }
  }
  lastView = now;
}
window.__gt = {
  ready: false,
  viewLog: [],
  find(key) { for (const el of document.querySelectorAll('*')) if (el.__gtKey === key) return el; return null; },
  keyOf(el) { for (let e = el; e; e = e.parentElement) if (e.__gtKey) return e.__gtKey; return null; },
  pathsOf(el) { for (let e = el; e; e = e.parentElement) if (e.__gtPaths && e.__gtPaths.length) return e.__gtPaths; return []; },
  displayed() {
    const out = new Set();
    for (const el of document.querySelectorAll('*')) for (const p of (el.__gtShows || [])) out.add(p);
    return [...out].sort();
  },
  viewLeaves,
  seqApplied: () => D.seq,
};

// ---- start -----------------------------------------------------------------
(async () => {
  CFG = await (await fetch('/api/config')).json();
  const b = await (await fetch('/api/bootstrap')).json();
  for (const inv of b.invoices) D.invoices.set(inv.id, inv);
  for (const v of b.vendors) D.vendors.set(v.id, v);
  D.batches = b.batches; D.seq = b.seq;
  S.bulk.value = CFG.categories[0];
  onRoute();
  window.__gt.ready = true;
  if (CFG.poll_interval_ms > 0) setInterval(() => sync(false), CFG.poll_interval_ms);
})();
})();
