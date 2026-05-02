'use strict';

// ── State ─────────────────────────────────────────────────────────
const state = {
  tests: new Map(),   // nodeid → { selected, status, duration, longrepr }
  totals: { total: 0, passed: 0, failed: 0, skipped: 0, running: 0 },
  running: false,
  runId: null,
  envVars: [],        // [{k, v}]
  logLines: 0,
  logOpen: false,
  ws: null,
  wsConnected: false,
  reconnectDelay: 1000,
  statusFilter: 'all',  // 'all' | 'passed' | 'failed' | 'skipped'
  textFilter:   '',
};

const STATUS = {
  IDLE: 'idle', QUEUED: 'queued', RUNNING: 'running',
  PASSED: 'passed', FAILED: 'failed', SKIPPED: 'skipped',
};

// ── DOM helpers ───────────────────────────────────────────────────
const $  = id => document.getElementById(id);
const mk = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls)  e.className   = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

// ── Preferences (localStorage) ────────────────────────────────────
function loadPrefs() {
  try {
    $('args').value    = localStorage.getItem('pw.args')    || '';
    $('workers').value = localStorage.getItem('pw.workers') || 1;
    state.envVars      = JSON.parse(localStorage.getItem('pw.envVars') || '[]');
    renderEnvVars();
    updateCommandPreview();
  } catch (_) {}
}

function savePrefs() {
  try {
    localStorage.setItem('pw.args',    $('args').value);
    localStorage.setItem('pw.workers', $('workers').value);
    localStorage.setItem('pw.envVars', JSON.stringify(state.envVars));
  } catch (_) {}
}

// ── WebSocket ─────────────────────────────────────────────────────
function initWS() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  state.ws = ws;

  ws.onopen = () => {
    state.wsConnected    = true;
    state.reconnectDelay = 1000;
    setWSIndicator(true);
  };

  ws.onmessage = ({ data }) => {
    try { handleWSMsg(JSON.parse(data)); } catch (e) { console.error(e); }
  };

  ws.onclose = ws.onerror = () => {
    state.wsConnected = false;
    state.ws          = null;
    setWSIndicator(false);
    setTimeout(initWS, state.reconnectDelay);
    state.reconnectDelay = Math.min(state.reconnectDelay * 1.5, 5000);
  };
}

function setWSIndicator(connected) {
  const el = $('ws-status');
  el.dataset.connected = connected;
  el.title = connected ? 'Connected' : 'Disconnected — reconnecting…';
}

// ── WebSocket message router ──────────────────────────────────────
function handleWSMsg(msg) {
  switch (msg.type) {
    case 'snapshot':      onSnapshot(msg);      break;
    case 'session_start': onSessionStart(msg);  break;
    case 'test_start':    onTestStart(msg);      break;
    case 'test_end':      onTestEnd(msg);        break;
    case 'session_end':   onSessionEnd(msg);     break;
    case 'log':           appendLog(msg.stream, msg.line); break;
  }
}

function onSnapshot(msg) {
  // Fired once on WS connect. If a run is in progress, sync test states.
  if (!msg.running) return;
  state.running = true;
  state.runId   = msg.run_id;
  for (const [nodeid, status] of Object.entries(msg.test_states || {})) {
    const t = state.tests.get(nodeid);
    if (t) { t.status = status; refreshRow(nodeid); }
  }
  patchTotals(msg.totals);
  setRunningUI(true);
}

function onSessionStart(msg) {
  state.running = true;
  state.runId   = msg.run_id;
  // Mark selected idle tests as queued; don't overwrite tests already in flight
  const terminal = new Set([STATUS.PASSED, STATUS.FAILED, STATUS.SKIPPED]);
  for (const [nodeid, t] of state.tests) {
    if (t.selected && !terminal.has(t.status)) {
      t.status   = STATUS.QUEUED;
      t.duration = null;
      t.longrepr = null;
      refreshRow(nodeid);
      const row = rowEl(nodeid);
      const detail = row && row.nextElementSibling;
      if (detail && detail.classList.contains('longrepr-detail')) detail.remove();
    }
  }
  state.totals = { total: msg.total, passed: 0, failed: 0, skipped: 0, running: 0 };
  updateSummary();
  setRunningUI(true);
  // Clear log for the new run
  $('log-output').textContent = '';
  state.logLines = 0;
  $('log-line-count').textContent = '';
}

function onTestStart(msg) {
  const t = state.tests.get(msg.nodeid);
  if (t) { t.status = STATUS.RUNNING; refreshRow(msg.nodeid); }
  state.totals.running = (state.totals.running || 0) + 1;
  updateSummary();
  applyFilter(); // running tests must always appear, even under a status filter
}

function onTestEnd(msg) {
  const t = state.tests.get(msg.nodeid);
  if (t) {
    t.status   = msg.outcome;
    t.duration = msg.duration;
    t.longrepr = msg.longrepr || null;
    refreshRow(msg.nodeid);
  }
  const tot = state.totals;
  tot.running = Math.max(0, (tot.running || 0) - 1);
  if (msg.outcome === 'passed')  tot.passed  = (tot.passed  || 0) + 1;
  if (msg.outcome === 'failed')  tot.failed  = (tot.failed  || 0) + 1;
  if (msg.outcome === 'skipped') tot.skipped = (tot.skipped || 0) + 1;
  updateSummary();
  applyFilter(); // test moved to a terminal status — re-evaluate filter
}

function onSessionEnd(msg) {
  state.running = false;
  state.runId   = null;
  if (msg.totals) { state.totals = msg.totals; updateSummary(); }
  // Any tests still queued/running were cancelled — reset them
  for (const [nodeid, t] of state.tests) {
    if (t.status === STATUS.QUEUED || t.status === STATUS.RUNNING) {
      t.status = STATUS.IDLE;
      refreshRow(nodeid);
    }
  }
  setRunningUI(false);
  applyFilter(); // cancelled/idle tests may no longer match active filter
}

// ── Test list ─────────────────────────────────────────────────────
function buildTestList(nodeids) {
  state.tests.clear();
  // Reset filters so the fresh list shows everything
  state.statusFilter = 'all';
  state.textFilter   = '';
  $('filter').value  = '';
  for (const el of document.querySelectorAll('.cnt[data-filter]')) {
    el.dataset.active = (el.dataset.filter === 'all') ? 'true' : 'false';
  }
  const container = $('test-list');
  container.innerHTML = '';

  if (nodeids.length === 0) {
    const msg = mk('div', 'empty-state');
    msg.id = 'empty-state';
    msg.textContent = 'No tests found for the given args.';
    container.appendChild(msg);
    updateSummary();
    updateCommandPreview();
    return;
  }

  // Initialise state entries
  for (const nodeid of nodeids) {
    state.tests.set(nodeid, { selected: true, status: STATUS.IDLE, duration: null, longrepr: null });
  }

  // Group by file (first segment before ::)
  const groups = new Map();
  for (const nodeid of nodeids) {
    const file = nodeid.split('::')[0];
    if (!groups.has(file)) groups.set(file, []);
    groups.get(file).push(nodeid);
  }

  const frag = document.createDocumentFragment();
  for (const [file, ids] of groups) {
    frag.appendChild(buildFileGroup(file, ids));
  }
  container.appendChild(frag);

  state.totals = { total: nodeids.length, passed: 0, failed: 0, skipped: 0, running: 0 };
  updateSummary();
  syncSelectAllCheckbox();
  updateCommandPreview();
}

function buildFileGroup(file, nodeids) {
  const group = mk('div', 'file-group');
  group.dataset.file = file;

  // Header
  const header = mk('div', 'file-header');
  const arrow  = mk('span', 'group-arrow', '▾');
  const name   = mk('span', 'file-name', file);
  const count  = mk('span', 'file-count', String(nodeids.length));
  header.append(arrow, name, count);
  header.addEventListener('click', () => {
    const body  = group.querySelector('.group-body');
    const isHidden = body.hidden;
    body.hidden = !isHidden;
    arrow.textContent = isHidden ? '▾' : '▸';
  });
  group.appendChild(header);

  // Rows
  const body = mk('div', 'group-body');
  for (const nodeid of nodeids) body.appendChild(buildTestRow(nodeid));
  group.appendChild(body);

  return group;
}

function buildTestRow(nodeid) {
  const row = mk('div', 'test-row');
  row.dataset.nodeid = nodeid;
  row.dataset.status = STATUS.IDLE;

  const chk = document.createElement('input');
  chk.type      = 'checkbox';
  chk.className = 'test-check';
  chk.checked   = true;
  chk.addEventListener('change', () => {
    state.tests.get(nodeid).selected = chk.checked;
    syncSelectAllCheckbox();
    updateCommandPreview();
  });

  const dot  = mk('span', 'status-dot');

  // Strip the file prefix; replace :: separators (class › method) for readability
  const parts    = nodeid.split('::');
  const nameText = (parts.length > 1 ? parts.slice(1) : parts).join(' › ');
  const nameEl   = mk('span', 'test-name', nameText);
  nameEl.title   = nodeid;  // full id on hover

  const dur = mk('span', 'test-duration');

  const expandBtn = mk('button', 'expand-btn', '▾');
  expandBtn.hidden = true;
  expandBtn.title  = 'Show error detail';
  expandBtn.addEventListener('click', e => { e.stopPropagation(); toggleLongrepr(nodeid, row); });

  row.append(chk, dot, nameEl, dur, expandBtn);
  return row;
}

function rowEl(nodeid) {
  return document.querySelector(`.test-row[data-nodeid="${CSS.escape(nodeid)}"]`);
}

function refreshRow(nodeid) {
  const row = rowEl(nodeid);
  if (!row) return;
  const t = state.tests.get(nodeid);
  if (!t) return;

  row.dataset.status = t.status;

  const dur = row.querySelector('.test-duration');
  dur.textContent = t.duration != null ? t.duration.toFixed(2) + 's' : '';

  const expandBtn = row.querySelector('.expand-btn');
  expandBtn.hidden = !(t.longrepr && t.status === STATUS.FAILED);
}

function toggleLongrepr(nodeid, row) {
  // Remove existing detail if present (toggle off)
  const existing = row.nextElementSibling;
  if (existing && existing.classList.contains('longrepr-detail')) {
    existing.remove();
    row.querySelector('.expand-btn').textContent = '▾';
    return;
  }
  const t = state.tests.get(nodeid);
  if (!t || !t.longrepr) return;

  const detail = mk('div', 'longrepr-detail');
  const pre    = mk('pre',  'longrepr-text', t.longrepr);
  detail.appendChild(pre);
  row.insertAdjacentElement('afterend', detail);
  row.querySelector('.expand-btn').textContent = '▴';
}

// ── Summary counters ──────────────────────────────────────────────
function updateSummary() {
  const { total, passed, failed, skipped } = state.totals;
  $('cnt-total').textContent   = `${total || 0} tests`;
  $('cnt-passed').textContent  = `${passed  || 0} passed`;
  $('cnt-failed').textContent  = `${failed  || 0} failed`;
  $('cnt-skipped').textContent = `${skipped || 0} skipped`;
  $('cnt-failed').dataset.nonzero = (failed || 0) > 0;
}

function patchTotals(patch) {
  if (!patch) return;
  Object.assign(state.totals, patch);
  updateSummary();
}

// ── Select-all checkbox ───────────────────────────────────────────
function visibleNodeids() {
  return [...document.querySelectorAll('.test-row')]
    .filter(r => !r.hidden)
    .map(r => r.dataset.nodeid);
}

function syncSelectAllCheckbox() {
  // "Select all" reflects only the currently-visible tests, so toggling it
  // while a filter is active selects/deselects only what the user can see.
  const visible = visibleNodeids();
  const sel     = visible.filter(id => state.tests.get(id)?.selected).length;
  const sa      = $('select-all');
  sa.checked       = visible.length > 0 && sel === visible.length;
  sa.indeterminate = sel > 0 && sel < visible.length;
}

// ── Command preview ───────────────────────────────────────────────
function stripKeywordFilters(argsStr) {
  // Mirror server-side _strip_keyword_filters: remove -k / -m and their values.
  const parts  = argsStr.trim().split(/\s+/);
  const result = [];
  let i = 0;
  while (i < parts.length) {
    const p = parts[i];
    if (p === '-k' || p === '-m') { i += 2; }          // skip flag + value
    else if (p.startsWith('-k=') || p.startsWith('-m=')) { i += 1; }
    else { result.push(p); i++; }
  }
  return result.join(' ');
}

function updateCommandPreview() {
  if (state.tests.size === 0) {
    $('command-text').textContent = 'pytest -p pytest_web.plugin';
    return;
  }
  const selected = [...state.tests.entries()]
    .filter(([, t]) => t.selected)
    .map(([id]) => id);

  const rawArgs = $('args').value.trim();
  const args    = stripKeywordFilters(rawArgs);  // -k/-m stripped for run
  const workers = +$('workers').value;
  let preview   = 'pytest';

  if (selected.length === 0) {
    preview += ' (no tests selected)';
  } else if (selected.length === state.tests.size) {
    // All selected — just show the args (less noise)
    if (args) preview += ' ' + args;
  } else if (selected.length <= 3) {
    preview += ' ' + selected.join(' ');
    if (args) preview += ' ' + args;
  } else {
    preview += ` [${selected.length} of ${state.tests.size} tests selected]`;
    if (args) preview += ' ' + args;
  }

  if (workers > 1) preview += ` -n ${workers}`;
  $('command-text').textContent = preview;
}

// ── Filter ────────────────────────────────────────────────────────
function rowMatchesFilters(row) {
  const nodeid = row.dataset.nodeid;
  // Text filter
  if (state.textFilter && !nodeid.toLowerCase().includes(state.textFilter)) return false;
  // Status filter — read from state (authoritative), NOT from row.dataset.status (stale DOM)
  if (state.statusFilter !== 'all') {
    const t = state.tests.get(nodeid);
    if (!t) return false;
    // Always show the currently-executing test regardless of active filter
    if (t.status === STATUS.RUNNING) return true;
    if (t.status !== state.statusFilter) return false;
  }
  return true;
}

function applyFilter() {
  for (const row of document.querySelectorAll('.test-row')) {
    row.hidden = !rowMatchesFilters(row);
  }
  for (const group of document.querySelectorAll('.file-group')) {
    const visible = [...group.querySelectorAll('.test-row')].some(r => !r.hidden);
    group.hidden = !visible;
  }
  syncSelectAllCheckbox();
}

function setStatusFilter(next) {
  // Click same filter again → reset to 'all'
  state.statusFilter = (state.statusFilter === next) ? 'all' : next;
  for (const el of document.querySelectorAll('.cnt')) {
    el.dataset.active = (el.dataset.filter === state.statusFilter) ? 'true' : 'false';
  }
  applyFilter();
}

// ── Env vars ──────────────────────────────────────────────────────
function stripQuotes(s) {
  s = s.trim();
  if (s.length >= 2 && ((s[0] === '"' && s.endsWith('"')) || (s[0] === "'" && s.endsWith("'")))) {
    return s.slice(1, -1);
  }
  return s;
}

function renderEnvVars() {
  const list = $('env-list');
  list.innerHTML = '';

  if (state.envVars.length === 0) return;

  // Column header row
  const header = mk('div', 'env-col-header');
  header.innerHTML =
    '<span class="env-col-label">Name</span>' +
    '<span class="env-col-label">Value</span>' +
    '<span class="env-col-label-rm"></span>';
  list.appendChild(header);

  state.envVars.forEach((ev, i) => {
    const row = mk('div', 'env-row');

    // ── Name field ────────────────────────────────────────────
    const keyInput       = mk('input');
    keyInput.type        = 'text';
    keyInput.className   = 'env-key';
    keyInput.placeholder = 'VARIABLE_NAME';
    keyInput.value       = ev.k;
    keyInput.autocomplete = 'off';
    keyInput.spellcheck   = false;
    keyInput.addEventListener('input', () => {
      // Save raw value while typing; split only when done (blur/paste).
      state.envVars[i].k = keyInput.value;
      savePrefs();
    });
    const trySplitKey = () => {
      const raw = keyInput.value;
      const eq  = raw.indexOf('=');
      if (eq > 0) {
        // User typed or pasted "NAME=value" into the name field — split it
        state.envVars[i].k = raw.slice(0, eq).trim();
        // Only overwrite value if it is currently empty
        if (!state.envVars[i].v) {
          state.envVars[i].v = stripQuotes(raw.slice(eq + 1));
        }
        savePrefs();
        renderEnvVars();
        return;
      }
      state.envVars[i].k = raw.trim();
      savePrefs();
    };
    keyInput.addEventListener('blur',  trySplitKey);
    keyInput.addEventListener('paste', () => setTimeout(trySplitKey, 0));

    // ── Value field ───────────────────────────────────────────
    const valInput        = mk('input');
    valInput.type         = 'text';
    valInput.className    = 'env-val';
    valInput.placeholder  = 'value';
    valInput.value        = ev.v;
    valInput.autocomplete = 'off';
    valInput.spellcheck   = false;
    valInput.addEventListener('input', () => {
      state.envVars[i].v = valInput.value;
      savePrefs();
    });
    valInput.addEventListener('blur', () => {
      // Strip accidental surrounding quotes — shell quotes are not needed here
      const clean = stripQuotes(valInput.value);
      state.envVars[i].v = clean;
      valInput.value = clean;
      savePrefs();
    });

    // ── Delete button ─────────────────────────────────────────
    const rm = mk('button', 'btn btn-ghost btn-xs env-rm', '✕');
    rm.title = 'Remove';
    rm.addEventListener('click', () => { state.envVars.splice(i, 1); renderEnvVars(); savePrefs(); });

    row.append(keyInput, valInput, rm);
    list.appendChild(row);
  });
}

// ── Log panel ─────────────────────────────────────────────────────
function appendLog(stream, line) {
  state.logLines++;
  if (!state.logOpen && stream === 'stderr') openLog();

  const pre  = $('log-output');
  const span = mk('span', `log-line log-${stream}`, line + '\n');
  pre.appendChild(span);
  pre.scrollTop = pre.scrollHeight;
  $('log-line-count').textContent = `(${state.logLines})`;
}

function openLog() {
  state.logOpen            = true;
  $('log-output').hidden   = false;
  $('log-arrow').textContent = '▾';
}

// ── Error banner ──────────────────────────────────────────────────
function showError(msg) {
  let banner = $('error-banner');
  if (!banner) {
    banner    = mk('div', 'error-banner');
    banner.id = 'error-banner';
    $('test-list').before(banner);
  }
  banner.textContent = msg;
  banner.hidden      = false;
}

function hideError() {
  const b = $('error-banner');
  if (b) b.hidden = true;
}

// ── API calls ─────────────────────────────────────────────────────
async function fetchTests() {
  const btn = $('btn-fetch');
  btn.disabled    = true;
  btn.textContent = 'Fetching…';
  hideError();

  try {
    const res  = await fetch('/discover', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ args: $('args').value.trim() }),
    });
    const data = await res.json();

    if (data.error) {
      showError('Collection failed:\n\n' + data.error);
      buildTestList([]);
    } else {
      buildTestList(data.nodeids || []);
    }
    savePrefs();
  } catch (e) {
    showError('Could not reach server: ' + e.message);
  } finally {
    btn.disabled    = false;
    btn.textContent = 'Fetch Tests';
  }
}

async function runSelected() {
  // Only run tests that are BOTH checked AND currently visible under the
  // active filter — so filtering to "Failed" and unchecking some lets the
  // user re-run a focused subset without dragging hidden checked tests in.
  const visible  = new Set(visibleNodeids());
  const selected = [...state.tests.entries()]
    .filter(([id, t]) => t.selected && visible.has(id))
    .map(([id]) => id);

  if (selected.length === 0) { showError('No tests selected.'); return; }
  hideError();

  const envObj = {};
  for (const { k, v } of state.envVars) {
    const key = k.trim();
    if (key) envObj[key] = stripQuotes((v || '').trim());
  }

  try {
    const res = await fetch('/run', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        nodeids: selected,
        workers: +$('workers').value,
        args:    $('args').value.trim(),
        env:     envObj,
      }),
    });

    if (res.status === 409) { showError('A run is already in progress.'); return; }
    const data = await res.json();
    // Update command preview to show the exact command that's running
    if (data.command) $('command-text').textContent = data.command;
    // Flip UI to running state immediately — don't wait on the WS round-trip
    // (cancel button must work even if a webhook is delayed or dropped)
    state.runId = data.run_id || null;
    setRunningUI(true);
    savePrefs();
  } catch (e) {
    showError('Failed to start run: ' + e.message);
  }
}

async function cancelRun() {
  try { await fetch('/cancel', { method: 'POST' }); } catch (_) {}
}

// ── UI state ──────────────────────────────────────────────────────
function setRunningUI(running) {
  state.running               = running;
  $('btn-run').disabled       = running;
  $('btn-cancel').disabled    = !running;
  $('btn-fetch').disabled     = running;
  $('btn-run').textContent    = running ? '⏳  Running…' : '▶  Run Selected';
}

// ── Param builder ─────────────────────────────────────────────────
async function loadProjectOptions() {
  // Fetch project-specific pytest options (from conftest / plugins) and inject
  // them as a "Project Options" optgroup so the dropdown reflects what's
  // actually available in the user's project, not just the pytest defaults.
  let opts = [];
  try {
    const res  = await fetch('/options');
    const data = await res.json();
    opts = data.options || [];
  } catch (_) { return; }

  if (!opts.length) return;

  const select  = $('param-select');
  // Drop any existing Project Options group (in case of re-fetch)
  select.querySelectorAll('optgroup[data-project="1"]').forEach(g => g.remove());

  // Skip options already present in the static dropdown so we don't duplicate
  const existing = new Set(
    [...select.querySelectorAll('option')].map(o => o.value)
  );
  const fresh = opts.filter(o => !existing.has(o.name));
  if (!fresh.length) return;

  const group = document.createElement('optgroup');
  group.label = 'Project Options';
  group.dataset.project = '1';
  for (const o of fresh) {
    const opt = document.createElement('option');
    opt.value = o.name;
    opt.dataset.type = o.type;
    if (o.type === 'value') opt.dataset.ph = 'value';
    opt.textContent = o.name;
    group.appendChild(opt);
  }
  select.appendChild(group);
}

function syncParamValueInput() {
  const opt      = $('param-select').selectedOptions[0];
  const isFlag   = opt && opt.dataset.type === 'flag';
  const valInput = $('param-value');
  valInput.disabled    = isFlag;
  valInput.placeholder = isFlag ? '(no value needed)' : (opt && opt.dataset.ph) || 'value';
  if (isFlag) valInput.value = '';
}

function addParam() {
  const opt    = $('param-select').selectedOptions[0];
  if (!opt) return;
  const isFlag = opt.dataset.type === 'flag';
  const name   = opt.value;
  const value  = $('param-value').value.trim();

  if (!isFlag && !value) {
    $('param-value').focus();
    return;
  }

  // Quote value if it contains spaces
  const safeVal  = value.includes(' ') ? `"${value}"` : value;
  const addition = isFlag ? name : `${name} ${safeVal}`;

  const bar   = $('args');
  bar.value   = bar.value.trim() ? bar.value.trimEnd() + ' ' + addition : addition;
  if (!isFlag) $('param-value').value = '';
  bar.focus();
  updateCommandPreview();
  savePrefs();
}

// ── Event wiring ──────────────────────────────────────────────────
function initListeners() {
  $('btn-fetch').addEventListener('click', fetchTests);
  $('btn-run').addEventListener('click', runSelected);
  $('btn-cancel').addEventListener('click', cancelRun);

  $('args').addEventListener('keydown', e => { if (e.key === 'Enter') fetchTests(); });
  $('args').addEventListener('input', updateCommandPreview);

  $('workers').addEventListener('input', () => { updateCommandPreview(); savePrefs(); });

  $('select-all').addEventListener('change', e => {
    // Toggle only the currently-visible tests, preserving selections of
    // hidden tests so switching filters doesn't lose prior state.
    const checked = e.target.checked;
    const visible = new Set(visibleNodeids());
    for (const [id, t] of state.tests) {
      if (visible.has(id)) t.selected = checked;
    }
    for (const row of document.querySelectorAll('.test-row')) {
      if (visible.has(row.dataset.nodeid)) {
        row.querySelector('.test-check').checked = checked;
      }
    }
    updateCommandPreview();
  });

  $('filter').addEventListener('input', e => {
    state.textFilter = e.target.value.toLowerCase();
    applyFilter();
  });

  for (const el of document.querySelectorAll('.cnt[data-filter]')) {
    el.addEventListener('click', () => setStatusFilter(el.dataset.filter));
  }

  $('btn-add-env').addEventListener('click', () => {
    state.envVars.push({ k: '', v: '' });
    renderEnvVars();
  });

  // Param builder
  $('param-select').addEventListener('change', syncParamValueInput);
  $('param-value').addEventListener('keydown', e => { if (e.key === 'Enter') addParam(); });
  $('btn-add-param').addEventListener('click', addParam);
  syncParamValueInput(); // set initial state

  $('btn-copy').addEventListener('click', () => {
    navigator.clipboard?.writeText($('command-text').textContent).then(() => {
      $('btn-copy').textContent = '✓';
      setTimeout(() => { $('btn-copy').textContent = '⎘'; }, 1400);
    });
  });

  $('log-toggle').addEventListener('click', () => {
    state.logOpen              = !state.logOpen;
    $('log-output').hidden     = !state.logOpen;
    $('log-arrow').textContent = state.logOpen ? '▾' : '▸';
  });
}

// ── Boot ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initListeners();
  loadPrefs();
  initWS();
  loadProjectOptions();
});
