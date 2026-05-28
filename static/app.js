// cube-power dashboard
const cardsEl = document.getElementById('cards');
const coordEl = document.getElementById('coord');
const logEl = document.getElementById('log');
const connEl = document.getElementById('conn');
const connText = document.getElementById('conntext');

const state = { devices: {}, coordinator: null, pvwatts: {} };

function fmt(n, d = 0) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return Number(n).toFixed(d);
}
function ago(ts) {
  if (!ts) return '—';
  const s = Math.max(0, (Date.now() / 1000) - ts);
  if (s < 60) return `${s.toFixed(0)}s ago`;
  if (s < 3600) return `${(s / 60).toFixed(0)}m ago`;
  return `${(s / 3600).toFixed(1)}h ago`;
}
function socColor(p) {
  if (p === null || p === undefined) return 'var(--dim)';
  if (p < 33) return 'var(--bad)';
  if (p < 45) return 'var(--warn)';
  return 'var(--ok)';
}

function renderCoord() {
  const c = state.coordinator;
  if (!c) { coordEl.innerHTML = '<span class="empty">waiting for coordinator…</span>'; return; }
  const cs = (k, v) => `<div class="c-stat"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  coordEl.innerHTML =
    cs('balance', `<span class="badge ${c.balance_state}">${c.balance_state}</span>`) +
    cs('cars', c.n_cars + (c.n_cars_measured ? '' : ' (assumed)')) +
    cs('units feeding', c.units_on) +
    cs('solar in', fmt(c.total_solar_w) + ' W') +
    cs('bus output', fmt(c.total_ac_out_w) + ' W') +
    cs('actuator', c.actuator_ready ? 'ready' : 'not mapped') +
    (c.note ? cs('note', c.note) : '');
}

function unitCard(d) {
  const s = d.state, role = d.role;
  const soc = s.soc_pct, acOn = !!s.ac_on;
  const want = state.coordinator && state.coordinator.desired_ac
    ? state.coordinator.desired_ac[s.unit_id] : undefined;
  const ttl = d.override_expires_at
    ? Math.max(0, (d.override_expires_at - Date.now() / 1000) / 3600) : null;
  const pv = state.pvwatts[s.unit_id];
  const expected = pv && pv.expected_w != null ? fmt(pv.expected_w) + ' W' : '—';
  return `
    <div class="card">
      <h2>${s.name}</h2>
      <div class="meta">${s.ip} · ${s.online ? 'online' : 'offline'} · ${s.mode || ''} · ${ago(s.updated_at)}</div>
      <div class="soc">
        <div class="pct" style="color:${socColor(soc)}">${soc == null ? '—' : fmt(soc) + '%'}</div>
        <div class="badge ${role}">${role}</div>
      </div>
      <div class="bar"><div style="width:${soc || 0}%;background:${socColor(soc)}"></div></div>
      <div class="stats">
        <div class="stat"><div class="k">solar in</div><div class="v">${fmt(s.solar_in_w)} W</div></div>
        <div class="stat"><div class="k">pvwatts expects</div><div class="v">${expected}</div></div>
        <div class="stat"><div class="k">ac out</div><div class="v">${fmt(s.ac_out_w)} W</div></div>
        <div class="stat"><div class="k">temp</div><div class="v">${s.temp_c == null ? '—' : fmt(s.temp_c, 1) + '°'}</div></div>
      </div>
      <div class="ac ${acOn ? 'on' : ''}"><span class="dot"></span>AC inverter ${acOn ? 'ON' : 'off'}` +
        (want !== undefined && want !== acOn ? ` <span style="color:var(--warn)">→ ${want ? 'ON' : 'off'}</span>` : '') +
      `</div>
      <div class="controls">
        <button class="primary" data-action="on" data-unit="${s.unit_id}">Force ON 48h</button>
        <button class="danger" data-action="off" data-unit="${s.unit_id}">Force OFF 48h</button>
        <button data-action="release" data-unit="${s.unit_id}">Release</button>
      </div>
      ${ttl != null ? `<div class="override-info">override → AC ${d.override_target ? 'ON' : 'OFF'} for ${ttl.toFixed(1)}h more</div>` : ''}
    </div>`;
}

function render() {
  const devs = Object.values(state.devices);
  cardsEl.innerHTML = devs.length
    ? devs.map(unitCard).join('')
    : `<div class="empty">no devices configured.<br><small>add devices.json and restart</small></div>`;
  cardsEl.querySelectorAll('button').forEach(b => b.addEventListener('click', onAction));
  renderCoord();
}

async function onAction(e) {
  const u = e.target.dataset.unit, act = e.target.dataset.action;
  e.target.disabled = true;
  try {
    if (act === 'on' || act === 'off') {
      await fetch(`/api/${u}/override`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ on: act === 'on', ttl_h: 48 })
      });
    } else if (act === 'release') {
      await fetch(`/api/${u}/override`, { method: 'DELETE' });
    }
  } finally {
    setTimeout(() => e.target.disabled = false, 500);
  }
}

function appendDecision(entry) {
  const t = new Date(entry.t * 1000).toLocaleTimeString();
  const div = document.createElement('div');
  div.className = 'entry';
  const who = entry.name || entry.unit || entry.source || '';
  div.innerHTML = `<span class="t">${t}</span> <b>${entry.source}</b> ${who} ` +
    `<span>${entry.reason || ''}</span>` +
    (entry.applied === true ? ' <span style="color:var(--ok)">✓</span>' : '') +
    (entry.dry_run ? ' <span style="color:var(--warn)">dry</span>' : '') +
    (entry.error ? ` <span style="color:var(--bad)">${entry.error}</span>` : '');
  logEl.prepend(div);
  while (logEl.children.length > 200) logEl.removeChild(logEl.lastChild);
}

async function loadInitialLog() {
  try {
    const r = await fetch('/api/log?limit=50');
    (await r.json()).reverse().forEach(appendDecision);
  } catch { }
}

function applySnapshot(snap) {
  state.devices = {};
  (snap.devices || []).forEach(d => state.devices[d.state.unit_id] = d);
  state.coordinator = snap.coordinator || null;
  state.pvwatts = snap.pvwatts || {};
  render();
}

let ws, backoff = 1000;
function connect() {
  ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/api/ws');
  ws.onopen = () => { connEl.classList.add('live'); connText.textContent = 'live'; backoff = 1000; };
  ws.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === 'snapshot') applySnapshot(ev.data);
    else if (ev.type === 'state') {
      const d = state.devices[ev.unit] || (state.devices[ev.unit] = {});
      d.state = ev.state;
      if (d.role === undefined) d.role = 'OFFLINE';
      render();
    } else if (ev.type === 'coordinator') {
      state.coordinator = ev.snapshot;
      render();
    } else if (ev.type === 'decision') {
      appendDecision(ev.entry);
    }
  };
  ws.onclose = () => {
    connEl.classList.remove('live');
    connText.textContent = 'reconnecting…';
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 30000);
  };
  ws.onerror = () => ws.close();
}

loadInitialLog().then(connect);
// periodic full refresh so unit roles (set by the coordinator) stay current
setInterval(async () => {
  try {
    const r = await fetch('/api/state');
    const snap = await r.json();
    if (snap.devices) applySnapshot(snap);
  } catch { }
}, 20000);
