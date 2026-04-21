/**
 * NetMon - Network Monitor Dashboard
 * Vanilla JS WebSocket client with real-time DOM updates and sparklines.
 */

// APP VERSION - bump this whenever shell behavior changes so old PWAs force-refresh
const APP_VERSION = 'v10';

// Self-heal: if an older service worker is cached, kill it and reload once
(async function killStaleCaches() {
  const stored = localStorage.getItem('netmon.app.version');
  if (stored === APP_VERSION) return; // already on current version

  try {
    if ('serviceWorker' in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map(r => r.unregister()));
    }
    if (self.caches) {
      const keys = await caches.keys();
      await Promise.all(keys.map(k => caches.delete(k)));
    }
  } catch {}
  localStorage.setItem('netmon.app.version', APP_VERSION);
  // Reload once with cache-bust, if this is an upgrade from a prior version
  if (stored && stored !== APP_VERSION) {
    location.replace(location.pathname + '?bust=' + Date.now());
  }
})();

// --- Configuration ---
// Auth: the server gates every /api/* request and the /ws upgrade behind
// NETMON_API_TOKEN. We prompt for the token on first load, cache it in
// localStorage, and attach it to every fetch + the websocket URL. The
// browser UI was previously reaching these endpoints without auth,
// which is why the dashboard hung on "Connecting…" after the auth
// hardening rolled out.
const TOKEN_KEY = 'netmon_api_token';
function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; }
}
function setToken(tok) {
  try {
    if (tok) localStorage.setItem(TOKEN_KEY, tok);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {}
}
function promptForToken(message) {
  // Use the URL fragment once if present (lets the Mac Server app
  // deep-link with #token=… so the user doesn't have to copy-paste).
  const hash = new URLSearchParams(location.hash.replace(/^#/, ''));
  const hashTok = hash.get('token');
  if (hashTok) {
    setToken(hashTok);
    // Scrub it from the URL so it doesn't get saved to history / shared.
    // Use window.history explicitly — a later `let history = {}` shadows
    // the global in this file, and TDZ means the bare `history` here
    // throws "cannot access before initialization".
    window.history.replaceState(null, '', location.pathname + location.search);
    return hashTok;
  }
  const existing = getToken();
  const prompt = message || 'Paste your NetMon API token (NETMON_API_TOKEN from the server .env):';
  const tok = window.prompt(prompt, existing);
  if (tok !== null && tok.trim()) {
    setToken(tok.trim());
    return tok.trim();
  }
  return existing;
}

// Build the WebSocket URL with the stored token as `?token=` since
// browsers can't set a custom Authorization header on a WebSocket.
function wsURL() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const tok = getToken();
  const q = tok ? `?token=${encodeURIComponent(tok)}` : '';
  return `${scheme}://${location.host}/ws${q}`;
}

// Wrapper around fetch() that adds Authorization: Bearer on every call.
// Pass-through for everything else, including response handling.
async function authFetch(input, init = {}) {
  const tok = getToken();
  const headers = new Headers(init.headers || {});
  if (tok) headers.set('Authorization', `Bearer ${tok}`);
  return fetch(input, { ...init, headers });
}

// Seed the token on startup — either from URL fragment, localStorage,
// or an interactive prompt if neither.
(function ensureToken() {
  const hash = new URLSearchParams(location.hash.replace(/^#/, ''));
  if (hash.get('token')) { promptForToken(); return; }
  if (!getToken()) { promptForToken(); }
})();

const RECONNECT_BASE = 1000;
const RECONNECT_MAX = 30000;
const SPARKLINE_POINTS = 120;

// --- State ---
let state = {};
let history = {};
let ws = null;
let reconnectDelay = RECONNECT_BASE;
let reconnectTimer = null;

// --- DOM Cache ---
const elCache = {};

function getEl(key, attr) {
  const cacheKey = `${attr}:${key}`;
  if (!elCache[cacheKey]) {
    elCache[cacheKey] = document.querySelectorAll(`[${attr}="${key}"]`);
  }
  return elCache[cacheKey];
}

// --- Formatters ---
function formatBps(bps) {
  if (bps == null || bps === '') return '--';
  const n = Number(bps);
  if (isNaN(n) || n === 0) return '0';
  if (n >= 1e9) return (n / 1e9).toFixed(1) + ' Gbps';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + ' Mbps';
  if (n >= 1e3) return (n / 1e3).toFixed(0) + ' Kbps';
  return n.toFixed(0) + ' bps';
}

function formatUptime(secs) {
  if (secs == null || secs === 0) return '--';
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function formatMs(ms) {
  if (ms == null) return '--';
  const n = Number(ms);
  if (isNaN(n) || n < 0) return '--';
  return n < 1 ? '<1 ms' : n.toFixed(1) + ' ms';
}

function formatPct(val) {
  if (val == null) return '--';
  return Number(val).toFixed(1) + '%';
}

function timeAgo(ts) {
  if (!ts) return '--';
  const diff = (Date.now() / 1000) - ts;
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}

function formatDbm(v) {
  if (v == null || v === '' || v === 0) return '--';
  const n = Number(v);
  if (isNaN(n)) return '--';
  return n.toFixed(0) + ' dBm';
}

function formatDb(v) {
  if (v == null || v === '') return '--';
  const n = Number(v);
  if (isNaN(n)) return '--';
  return n.toFixed(1) + ' dB';
}

function formatValue(val, fmt) {
  switch (fmt) {
    case 'bps': return formatBps(val);
    case 'uptime': return formatUptime(val);
    case 'ms': return formatMs(val);
    case 'pct': return formatPct(val);
    case 'dbm': return formatDbm(val);
    case 'db': return formatDb(val);
    case 'status-badge': return makeBadge(val, 'status-badge');
    case 'sf-badge': return makeBadge(val, 'sf-badge');
    case 'sf-badge-lg': return makeBadge(val, 'sf-badge-lg');
    default: return val != null ? String(val) : '--';
  }
}

function makeBadge(val, cls) {
  if (!val || val === '--') return `<span class="${cls} unknown">--</span>`;
  const v = String(val).toLowerCase().replace(/\s+/g, '_');
  return `<span class="${cls} ${v}">${val}</span>`;
}

// --- DOM Updates ---
function updateDOM(data) {
  for (const [key, val] of Object.entries(data)) {
    state[key] = val;

    // Update text elements
    getEl(key, 'data-text').forEach(el => {
      const fmt = el.getAttribute('data-format');
      if (fmt && (fmt.includes('badge'))) {
        el.innerHTML = formatValue(val, fmt);
      } else {
        el.textContent = formatValue(val, fmt);
      }
    });

    // Update status dots
    getEl(key, 'data-key').forEach(el => {
      if (el.classList.contains('status-dot')) {
        el.className = 'status-dot';
        if (val) el.classList.add(String(val).toLowerCase());
      }
    });

    // Update bars
    getEl(key, 'data-bar').forEach(el => {
      const max = Number(el.getAttribute('data-max') || 100);
      const pct = Math.min(100, (Number(val) / max) * 100);
      el.style.width = pct + '%';
      el.classList.remove('warn', 'crit');
      if (pct > 80) el.classList.add('crit');
      else if (pct > 60) el.classList.add('warn');
    });

    // Update history for sparklines
    if (typeof val === 'number') {
      if (!history[key]) history[key] = [];
      history[key].push({ t: Date.now() / 1000, v: val });
      if (history[key].length > SPARKLINE_POINTS) {
        history[key] = history[key].slice(-SPARKLINE_POINTS);
      }
    }
  }

  // Device card states
  updateCardState('card-udm', 'udm.status');
  updateCardState('card-bal310', 'bal310.status');
  updateCardState('card-br1', 'br1.status');

  // BR1 subtitle
  const br1Sub = document.getElementById('br1-subtitle');
  if (br1Sub) {
    if (state['br1.status'] === 'unreachable') {
      br1Sub.textContent = 'Last seen: ' + timeAgo(state['br1.last_seen']);
    } else {
      br1Sub.textContent = formatUptime(state['br1.uptime']);
    }
  }

  // Internet status badge
  updateInternetStatus();

  // Last update time
  const lu = document.getElementById('last-update');
  if (lu) lu.textContent = new Date().toLocaleTimeString();

  // Build ping targets if we have data
  buildPingTargets();

  // Redraw sparklines
  drawAllSparklines();
}

function updateCardState(cardId, statusKey) {
  const card = document.getElementById(cardId);
  if (!card) return;
  const s = state[statusKey];
  card.classList.remove('unreachable');
  if (s === 'unreachable' || s === 'offline') {
    card.classList.add('unreachable');
  }
  // Flash animation when coming online
  if (s === 'online' && card.dataset.prevStatus === 'unreachable') {
    card.classList.add('flash-online');
    setTimeout(() => card.classList.remove('flash-online'), 1500);
  }
  card.dataset.prevStatus = s || '';
}

function updateInternetStatus() {
  const el = document.getElementById('internet-status');
  if (!el) return;
  // Check ping to 8.8.8.8 and 1.1.1.1
  const g = state['ping.8_8_8_8.status'];
  const c = state['ping.1_1_1_1.status'];
  if (g === 'ok' && c === 'ok') {
    el.textContent = 'INTERNET OK';
    el.className = 'badge badge-internet-ok';
  } else if (g === 'ok' || c === 'ok') {
    el.textContent = 'DEGRADED';
    el.className = 'badge badge-internet-degraded';
  } else if (g || c) {
    el.textContent = 'INTERNET DOWN';
    el.className = 'badge badge-internet-down';
  } else {
    el.textContent = '--';
    el.className = 'badge';
  }
}

// Ping device prefixes we scan state for. "ping" is the legacy hardcoded
// prefix; the others are populated at startup from /api/devices (any
// device with kind === "icmp_ping" gets its id added here). Without this
// discovery step the dashboard only rendered the legacy prefix and
// missed every driver-registered ICMP device.
let _icmpPingPrefixes = ['ping'];
// Router devices (Peplink + UniFi kinds) — these publish SSH-streamer
// ping state under `<deviceId>.<host_underscored>.{latency_ms,loss_pct,
// name,host,status,source}`. We render them in the Router Ping
// Monitors section so the user sees packet loss from each router's
// vantage point (lower latency + higher fidelity than polled metrics).
let _routerDevices = [];  // [{id, displayName}]
async function loadIcmpPingDevices() {
  try {
    const r = await authFetch('/api/devices');
    if (!r.ok) return;
    const body = await r.json();
    const devs = body.devices || [];
    _icmpPingPrefixes = Array.from(new Set([
      'ping',
      ...devs
        .filter(d => (d.kind || '').replace(/^legacy_/, '') === 'icmp_ping')
        .map(d => d.id),
    ]));
    _routerDevices = devs
      .filter(d => {
        const k = (d.kind || '').replace(/^legacy_/, '');
        return k === 'peplink_router' || k === 'unifi_network';
      })
      .map(d => ({ id: d.id, displayName: d.display_name || d.id }));
  } catch {}
}
loadIcmpPingDevices();

// --- Ping Target Cards ---
function buildPingTargets() {
  const grid = document.getElementById('ping-grid');
  const br1Grid = document.getElementById('br1-internet-grid');
  if (!grid) return;

  // Find all home ping targets currently in state (skip hidden ones).
  // Scan every ICMP-device prefix we know about — legacy `ping.*` plus
  // whatever came back from /api/devices. `targets` now carries the
  // full prefix so downstream code can reconstruct the state keys.
  const targets = [];  // { prefix, tkey, card_id }
  const seen = new Set();
  for (const key of Object.keys(state)) {
    for (const prefix of _icmpPingPrefixes) {
      const re = new RegExp(`^${prefix.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&')}\\.(.+)\\.name$`);
      const m = key.match(re);
      if (!m) continue;
      const tkey = m[1];
      if (state[`${prefix}.${tkey}.hidden`]) continue;
      const id = `${prefix}-${tkey}`;
      if (seen.has(id)) continue;
      seen.add(id);
      targets.push({ prefix, tkey, id });
    }
  }

  // Find all BR1->internet targets
  const br1Targets = [];
  for (const key of Object.keys(state)) {
    const m = key.match(/^br1_internet\.(.+)\.name$/);
    if (m) br1Targets.push(m[1]);
  }

  // Remove stale ping cards that no longer match (e.g., became hidden
  // or the originating device got removed from /api/devices).
  const validHomeIds = new Set(targets.map(t => `ping-${t.id}`));
  for (const card of grid.querySelectorAll('.ping-card')) {
    if (!validHomeIds.has(card.id)) card.remove();
  }

  // Ensure every home target has a corresponding card; add missing ones
  let homeAddedAny = false;
  for (const t of targets) {
    if (document.getElementById(`ping-${t.id}`)) continue;
    homeAddedAny = true;
  }
  let br1AddedAny = false;
  for (const tkey of br1Targets) {
    if (document.getElementById(`br1-internet-${tkey}`)) continue;
    br1AddedAny = true;
  }
  if (!homeAddedAny && !br1AddedAny) return;
  if (targets.length === 0 && br1Targets.length === 0) return;

  for (const t of targets) {
    if (document.getElementById(`ping-${t.id}`)) continue;
    const p = t.prefix;
    const k = t.tkey;
    const card = document.createElement('div');
    card.className = 'ping-card';
    card.id = `ping-${t.id}`;
    card.innerHTML = `
      <div class="ping-name" data-text="${p}.${k}.name">${state[`${p}.${k}.name`] || k}</div>
      <div class="ping-host" data-text="${p}.${k}.host">${state[`${p}.${k}.host`] || ''}</div>
      <div class="ping-latency" data-text="${p}.${k}.latency_ms" data-format="ms" id="ping-lat-${t.id}">--</div>
      <div class="ping-stats">
        Jitter: <span data-text="${p}.${k}.jitter_ms" data-format="ms">--</span> |
        Loss: <span data-text="${p}.${k}.packet_loss_pct" data-format="pct">--</span>
      </div>
      <canvas data-sparkline="${p}.${k}.latency_ms" width="160" height="36"></canvas>
    `;
    grid.appendChild(card);
  }

  if (br1Grid) {
    for (const tkey of br1Targets) {
      if (document.getElementById(`br1-internet-${tkey}`)) continue;
      const card = document.createElement('div');
      card.className = 'ping-card';
      card.id = `br1-internet-${tkey}`;
      card.innerHTML = `
        <div class="ping-name" data-text="br1_internet.${tkey}.name">${state[`br1_internet.${tkey}.name`] || tkey}</div>
        <div class="ping-host" data-text="br1_internet.${tkey}.host">${state[`br1_internet.${tkey}.host`] || ''}</div>
        <div class="ping-latency" data-text="br1_internet.${tkey}.latency_ms" data-format="ms" id="br1int-lat-${tkey}">--</div>
        <div class="ping-stats">
          Jitter: <span data-text="br1_internet.${tkey}.jitter_ms" data-format="ms">--</span> |
          Loss: <span data-text="br1_internet.${tkey}.loss_pct" data-format="pct">--</span>
        </div>
        <canvas data-sparkline="br1_internet.${tkey}.latency_ms" width="160" height="36"></canvas>
      `;
      br1Grid.appendChild(card);
    }
  }

  buildRouterPingTargets();

  // Clear cache so new elements are found
  Object.keys(elCache).forEach(k => delete elCache[k]);

  // Re-apply current state to new elements
  updateDOM(state);
}

// Keys the router publishes under its own id that are NOT ping targets
// — these segments appear as `<deviceId>.<segment>.*` but don't mean a
// ping target. Exclude them from the router-ping discovery scan.
const _ROUTER_NON_PING_SEGMENTS = new Set([
  'wan1', 'wan2', 'wan3', 'wan4', 'wan5', 'wan6',
  'internet', 'status', 'model', 'version', 'uptime',
  'cpu', 'mem', 'clients', 'active_wan_ip', 'active_isp_name',
  'active_isp_org', 'active_asn', 'wlan_clients', 'lan_clients',
]);

function buildRouterPingTargets() {
  const card = document.getElementById('router-pings-card');
  const body = document.getElementById('router-pings-body');
  if (!card || !body) return;

  // For each router device, scan state for `<id>.<segment>.host` keys
  // (the host field is unique to ping targets — WANs don't publish it).
  // Group by device, render a subsection per device.
  const byDevice = new Map();  // id -> [{tkey, host, name}]
  for (const dev of _routerDevices) {
    const prefix = dev.id;
    // Escape prefix for regex; only letters/digits/_/- expected but be safe.
    const escPrefix = prefix.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = new RegExp(`^${escPrefix}\\.([^.]+)\\.host$`);
    const targets = [];
    for (const key of Object.keys(state)) {
      const m = key.match(re);
      if (!m) continue;
      const seg = m[1];
      if (_ROUTER_NON_PING_SEGMENTS.has(seg)) continue;
      // Skip WAN-flavored monitor keys like `udm.wan1.mon.1_1_1_1.*`
      // (different shape, handled by UDM Internet Monitors section).
      if (seg.startsWith('wan')) continue;
      targets.push({
        tkey: seg,
        host: state[key] || seg,
        name: state[`${prefix}.${seg}.name`] || state[key] || seg,
      });
    }
    if (targets.length) byDevice.set(dev, targets);
  }

  if (byDevice.size === 0) {
    card.hidden = true;
    body.innerHTML = '';
    return;
  }
  card.hidden = false;

  // Rebuild from scratch — the set of routers + targets is small and
  // rarely changes, and incremental DOM patching here isn't worth the
  // complexity when updateDOM() handles per-value updates anyway.
  const existingIds = new Set(
    Array.from(body.querySelectorAll('[data-ping-card-id]'))
      .map(el => el.getAttribute('data-ping-card-id'))
  );
  const neededIds = new Set();
  for (const [dev, targets] of byDevice) {
    const sectionId = `router-ping-section-${dev.id}`;
    let section = document.getElementById(sectionId);
    if (!section) {
      section = document.createElement('div');
      section.id = sectionId;
      section.className = 'router-ping-section';
      section.innerHTML = `
        <div class="router-ping-header">
          <span class="router-ping-title">${dev.displayName}</span>
          <span class="text-muted" style="font-size:0.7rem; margin-left:8px">${dev.id}</span>
        </div>
        <div class="card-body ping-grid" id="router-ping-grid-${dev.id}"></div>
      `;
      body.appendChild(section);
    }
    const grid = section.querySelector('.ping-grid');
    for (const t of targets) {
      const cardId = `rping-${dev.id}-${t.tkey}`;
      neededIds.add(cardId);
      if (document.getElementById(cardId)) continue;
      const c = document.createElement('div');
      c.className = 'ping-card';
      c.id = cardId;
      c.setAttribute('data-ping-card-id', cardId);
      c.innerHTML = `
        <div class="ping-name" data-text="${dev.id}.${t.tkey}.name">${t.name}</div>
        <div class="ping-host" data-text="${dev.id}.${t.tkey}.host">${t.host}</div>
        <div class="ping-latency" data-text="${dev.id}.${t.tkey}.latency_ms" data-format="ms">--</div>
        <div class="ping-stats">
          Loss: <span data-text="${dev.id}.${t.tkey}.loss_pct" data-format="pct">--</span>
        </div>
        <canvas data-sparkline="${dev.id}.${t.tkey}.latency_ms" width="160" height="36"></canvas>
      `;
      grid.appendChild(c);
    }
  }
  // Remove cards for targets that no longer publish state.
  for (const id of existingIds) {
    if (!neededIds.has(id)) {
      const el = document.getElementById(id);
      if (el) el.remove();
    }
  }
}

// --- Sparklines ---
function drawSparkline(canvas, dataKey) {
  const data = history[dataKey];
  if (!data || data.length < 2) return;

  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = Math.floor(rect.width) || 160;
  const h = Math.floor(rect.height) || 36;

  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    ctx.scale(dpr, dpr);
  }

  ctx.clearRect(0, 0, w, h);

  const values = data.map(d => d.v);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;

  // Gradient line
  const latest = values[values.length - 1];
  let color;
  if (dataKey.includes('latency') || dataKey.includes('packet_loss')) {
    color = latest > 150 ? '#f85149' : latest > 50 ? '#d29922' : '#3fb950';
  } else {
    color = '#58a6ff';
  }

  ctx.beginPath();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.lineJoin = 'round';

  for (let i = 0; i < values.length; i++) {
    const x = (i / (values.length - 1)) * w;
    const y = h - ((values[i] - min) / range) * (h - 4) - 2;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();

  // Fill under line
  ctx.lineTo(w, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  ctx.fillStyle = color.replace(')', ',0.1)').replace('rgb', 'rgba');
  if (color.startsWith('#')) {
    const r = parseInt(color.slice(1, 3), 16);
    const g = parseInt(color.slice(3, 5), 16);
    const b = parseInt(color.slice(5, 7), 16);
    ctx.fillStyle = `rgba(${r},${g},${b},0.1)`;
  }
  ctx.fill();
}

let _sparklinePending = false;
function drawAllSparklines() {
  // Coalesce rapid updates: only redraw once per animation frame
  if (_sparklinePending) return;
  _sparklinePending = true;
  requestAnimationFrame(() => {
    _sparklinePending = false;
    document.querySelectorAll('canvas[data-sparkline]').forEach(canvas => {
      const key = canvas.getAttribute('data-sparkline');
      drawSparkline(canvas, key);
    });
  });
}

// --- Ping card coloring ---
function _colorize(el, status, lat) {
  el.classList.remove('good', 'warn', 'bad', 'timeout');
  if (status === 'timeout' || status === 'error' || lat == null || lat < 0) {
    el.classList.add('timeout');
    el.textContent = 'TIMEOUT';
  } else if (lat > 150) {
    el.classList.add('bad');
  } else if (lat > 50) {
    el.classList.add('warn');
  } else {
    el.classList.add('good');
  }
}

// Map a numeric metric into a 1-5 signal level
function signalLevelRSRP(rsrp) {
  if (rsrp == null || rsrp === 0 || isNaN(Number(rsrp))) return 0;
  const n = Number(rsrp);
  if (n > -80) return 5;        // excellent
  if (n > -90) return 4;        // good
  if (n > -100) return 3;       // fair
  if (n > -110) return 2;       // poor
  return 1;                     // very poor
}

function signalLevelSINR(sinr) {
  if (sinr == null || isNaN(Number(sinr))) return 0;
  const n = Number(sinr);
  if (n >= 20) return 5;
  if (n >= 13) return 4;
  if (n >= 5) return 3;
  if (n >= 0) return 2;
  return 1;
}

function setBars(el, level) {
  if (!el) return;
  el.classList.remove('lvl-1', 'lvl-2', 'lvl-3', 'lvl-4', 'lvl-5');
  if (level > 0) el.classList.add('lvl-' + level);
}

function updateWanLatencies() {
  for (const el of document.querySelectorAll('.wan-latency')) {
    const key = el.dataset.latKey;
    const v = key ? state[key] : null;
    el.classList.remove('good', 'warn', 'bad');
    if (v == null || v === '' || isNaN(Number(v)) || Number(v) <= 0) {
      el.textContent = '--';
      continue;
    }
    const n = Number(v);
    el.textContent = n.toFixed(0) + ' ms';
    if (n > 150) el.classList.add('bad');
    else if (n > 60) el.classList.add('warn');
    else el.classList.add('good');
  }
}

function updateSfToggle() {
  const btn = document.getElementById('sf-toggle-btn');
  if (!btn) return;
  if (btn.dataset.wired !== '1') {
    btn.dataset.wired = '1';
    btn.addEventListener('click', async () => {
      const sfStatus = state['br1.sf.status'];
      // If currently connected/connecting -> disable; else enable.
      const isOn = sfStatus === 'connected' || sfStatus === 'connecting' || sfStatus === 'establishing';
      const msg = isOn
        ? 'Disable SpeedFusion tunnel on BR1?\n\nTraffic from the truck LAN will drop the tunnel and go direct to the internet via BR1\'s WANs.\nRe-enabling restores the tunnel (takes a few seconds to reconnect).'
        : 'Re-enable the SpeedFusion tunnel on BR1?\n\nTraffic will resume routing through the home fiber (exit IP returns to home).';
      if (!confirm(msg)) return;
      btn.disabled = true;
      const origText = btn.textContent;
      btn.textContent = isOn ? 'disabling…' : 'enabling…';
      try {
        const r = await authFetch('/api/control/br1/sf/enable', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({enable: !isOn, profile_id: state['br1.sf.profile_id'] || 1}),
        });
        if (!r.ok) throw new Error(await r.text());
        btn.textContent = '✓';
        setTimeout(() => { btn.disabled = false; }, 1500);
      } catch (e) {
        alert('SF toggle failed: ' + e.message);
        btn.textContent = origText;
        btn.disabled = false;
      }
    });
  }
  const sfStatus = (state['br1.sf.status'] || '').toLowerCase();
  const br1Status = (state['br1.status'] || '').toLowerCase();
  if (br1Status === 'unreachable') {
    btn.textContent = 'BR1 offline';
    btn.className = 'sf-toggle-btn';
    btn.disabled = true;
    return;
  }
  btn.disabled = false;
  if (sfStatus === 'connected' || sfStatus === 'connecting' || sfStatus === 'establishing') {
    btn.textContent = 'Tunnel ON · click to bypass';
    btn.className = 'sf-toggle-btn sf-on';
  } else if (sfStatus === 'down' || sfStatus === 'disconnected') {
    btn.textContent = 'Tunnel OFF · click to enable';
    btn.className = 'sf-toggle-btn sf-off';
  } else {
    btn.textContent = `Tunnel: ${sfStatus || 'unknown'}`;
    btn.className = 'sf-toggle-btn';
  }
}

function updateSpeedFusionRisk() {
  // Label showing the tunnel's UDM WAN dependency
  const depLabel = document.getElementById('sf-dep-label');
  const depWan = state['bal310.sf.depends_on_udm_wan'];
  if (depLabel) {
    depLabel.textContent = depWan && depWan > 0 ? `via UDM WAN${depWan}` : '';
  }
  // Risk banner on SpeedFusion card
  const sfBanner = document.getElementById('sf-risk-banner');
  if (sfBanner) {
    if (state['bal310.sf.at_risk']) {
      sfBanner.textContent = state['bal310.sf.risk_reason'] || 'Tunnel at risk';
      sfBanner.style.display = 'block';
    } else {
      sfBanner.style.display = 'none';
    }
  }
  // Mirror on BR1 card so it's obvious the truck going offline is expected
  const br1Banner = document.getElementById('br1-risk-banner');
  if (br1Banner) {
    if (state['bal310.sf.at_risk']) {
      br1Banner.textContent = `Tunnel at risk — if BR1 drops, it can't reconnect until UDM WAN${depWan} is back`;
      br1Banner.style.display = 'block';
    } else {
      br1Banner.style.display = 'none';
    }
  }
}

function updateCellularStyling() {
  // Carrier block: assign brand class
  const block = document.getElementById('carrier-block');
  const logo = document.getElementById('carrier-logo');
  const op = (state['br1.wan2.operator'] || '').toLowerCase();
  let activeCarrier = null;
  if (block && logo) {
    block.classList.remove('verizon', 'att', 'tmobile');
    let initials = '?';
    if (op.includes('verizon')) { block.classList.add('verizon'); initials = 'vz'; activeCarrier = 'verizon'; }
    else if (op.includes('at&t') || op.includes('att')) { block.classList.add('att'); initials = 'att'; activeCarrier = 'att'; }
    else if (op.includes('t-mobile') || op.includes('tmobile')) { block.classList.add('tmobile'); initials = 'T'; activeCarrier = 'tmobile'; }
    else if (op) { initials = op[0].toUpperCase(); }
    logo.textContent = initials;
  }
  // Highlight the carrier button that's either actually-active or pending
  document.querySelectorAll('.carrier-btn').forEach(b => {
    const isPending = pendingCarrier && b.dataset.carrier === pendingCarrier;
    const isActive = !pendingCarrier && activeCarrier && b.dataset.carrier === activeCarrier;
    b.classList.toggle('active', Boolean(isActive));
    b.classList.toggle('pending', Boolean(isPending));
  });
  // Clear pending once the modem actually registered on the requested carrier
  if (pendingCarrier) {
    const match = {verizon: 'verizon', att: 'att', tmobile: 'tmobile'};
    if (pendingCarrier === 'auto') {
      // "auto" is the pending state until the modem reconnects to any carrier
      // (can't really know when it has "applied auto" so we let the timeout clear it)
    } else if (activeCarrier === match[pendingCarrier]) {
      pendingCarrier = null;
    }
  }
  // Signal bars
  setBars(document.getElementById('rsrp-bars'), signalLevelRSRP(state['br1.wan2.rsrp']));
  setBars(document.getElementById('sinr-bars'), signalLevelSINR(state['br1.wan2.sinr']));
  // Per-band table
  updateBandTable();
}

function _sinrClass(v) {
  const n = Number(v);
  if (isNaN(n)) return '';
  if (n >= 13) return 'sig-good';
  if (n >= 0) return 'sig-warn';
  return 'sig-bad';
}
function _rsrpClass(v) {
  const n = Number(v);
  if (isNaN(n) || n === 0) return '';
  if (n > -90) return 'sig-good';
  if (n > -105) return 'sig-warn';
  return 'sig-bad';
}

// Pretty-print MB as KB/MB/GB
function formatMB(mb) {
  if (mb == null || isNaN(Number(mb))) return '--';
  const n = Number(mb);
  if (n < 1) return (n * 1024).toFixed(0) + ' KB';
  if (n < 1024) return n.toFixed(1) + ' MB';
  return (n / 1024).toFixed(2) + ' GB';
}

function drawUsageBars(canvas, series) {
  if (!canvas || !Array.isArray(series) || !series.length) return;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = Math.floor(rect.width) || 300;
  const h = Math.floor(rect.height) || 48;
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width = w * dpr; canvas.height = h * dpr;
    canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
    ctx.scale(dpr, dpr);
  }
  ctx.clearRect(0, 0, w, h);

  const n = series.length;
  const maxTotal = Math.max(1, ...series.map(p => (p.up_mb || 0) + (p.down_mb || 0)));
  const barGap = 3;
  const barW = Math.max(4, (w - (n - 1) * barGap) / n);

  for (let i = 0; i < n; i++) {
    const p = series[i];
    const up = p.up_mb || 0;
    const down = p.down_mb || 0;
    const total = up + down;
    const hTotal = (total / maxTotal) * (h - 4);
    const hDown = (down / maxTotal) * (h - 4);
    const x = i * (barW + barGap);
    const y = h - hTotal;

    // Down (bottom of bar) - blue
    ctx.fillStyle = '#58a6ff';
    ctx.fillRect(x, h - hDown, barW, hDown);
    // Up (stacked on top of down) - purple
    ctx.fillStyle = '#bc8cff';
    ctx.fillRect(x, y, barW, hTotal - hDown);
  }
}

function updateUsageWidget() {
  for (const w of ['wan1', 'wan2']) {
    const today = document.getElementById(`usage-${w}-today`);
    const seven = document.getElementById(`usage-${w}-7d`);
    const month = document.getElementById(`usage-${w}-month`);
    if (today) {
      const total = (state[`ic2.br1.${w}.usage_today_up_mb`] || 0) + (state[`ic2.br1.${w}.usage_today_down_mb`] || 0);
      today.textContent = formatMB(total);
    }
    if (seven) {
      const total = (state[`ic2.br1.${w}.usage_7d_up_mb`] || 0) + (state[`ic2.br1.${w}.usage_7d_down_mb`] || 0);
      seven.textContent = formatMB(total);
    }
    // Draw chart
    const canvas = document.getElementById(`usage-${w}-chart`);
    if (canvas) drawUsageBars(canvas, state[`ic2.br1.${w}.usage_series`]);
    // Monthly pill is per-WAN
    if (month) {
      const mUp = state[`ic2.br1.${w}.usage_month_up_mb`];
      const mDown = state[`ic2.br1.${w}.usage_month_down_mb`];
      const mLabel = state[`ic2.br1.${w}.usage_month_label`];
      if (mUp != null && mDown != null) {
        month.textContent = `${mLabel || 'Month'}: ${formatMB(mUp + mDown)}`;
      }
    }
  }
}

function _formatEventTs(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    const now = new Date();
    const diffMs = now - d;
    const diffMin = diffMs / 60000;
    if (diffMin < 1) return 'just now';
    if (diffMin < 60) return Math.floor(diffMin) + 'm ago';
    if (diffMin < 1440) return Math.floor(diffMin / 60) + 'h ago';
    return d.toLocaleDateString('en-US', {month: 'short', day: 'numeric'}) + ' '
      + d.toLocaleTimeString('en-US', {hour: '2-digit', minute: '2-digit', hour12: false});
  } catch { return iso; }
}

function updateEventLog() {
  const tbody = document.getElementById('ic2-events-tbody');
  const pill = document.getElementById('event-count-pill');
  if (!tbody) return;
  const events = state['ic2.br1.events'];
  const n = Array.isArray(events) ? events.length : 0;
  if (pill) pill.textContent = n > 0 ? `${n} event${n === 1 ? '' : 's'}` : '—';
  if (!Array.isArray(events) || !events.length) {
    tbody.innerHTML = '<tr><td colspan="4" style="color:var(--text-muted)">No events</td></tr>';
    return;
  }
  const rows = events.slice(0, 30).map(e => {
    const t = (e.type || '').toLowerCase();
    const cls = t === 'wan' ? 'evt-wan'
              : t.includes('cellular') ? 'evt-cellular'
              : t === 'device' ? 'evt-device'
              : t.includes('pepvpn') || t.includes('speedfusion') ? 'evt-pepvpn'
              : '';
    const loc = (e.lat != null && e.lng != null && (e.lat !== 0 || e.lng !== 0))
      ? `${Number(e.lat).toFixed(4)}, ${Number(e.lng).toFixed(4)}` : '';
    return `<tr>
      <td class="col-ts">${_formatEventTs(e.ts)}</td>
      <td class="col-type"><span class="evt-badge ${cls}">${e.type || '—'}</span></td>
      <td>${e.detail || ''}</td>
      <td class="col-loc">${loc}</td>
    </tr>`;
  });
  tbody.innerHTML = rows.join('');
}

function _latClass(v) {
  if (v == null || v === '' || isNaN(Number(v)) || Number(v) <= 0) return 'lat-off';
  const n = Number(v);
  if (n > 150) return 'lat-bad';
  if (n > 60) return 'lat-warn';
  return 'lat-good';
}

// Nicely format a target hostname/IP for display
function _targetLabel(target) {
  const friendly = {
    '1.1.1.1': 'Cloudflare (1.1.1.1)',
    '8.8.8.8': 'Google DNS (8.8.8.8)',
    'www.microsoft.com': 'Microsoft',
    'google.com': 'Google',
    'ping.ui.com': 'UniFi',
  };
  return friendly[target] || target;
}

function updateUdmMonitorTable() {
  const tbody = document.getElementById('udm-mon-tbody');
  if (!tbody) return;

  // Collect all targets seen across both WANs
  const targets = new Set();
  for (const k of Object.keys(state)) {
    const m = k.match(/^udm\.wan[12]\.mon\.(.+)\.target$/);
    if (m) targets.add(state[k]);
  }
  if (!targets.size) {
    tbody.innerHTML = '<tr><td colspan="3" style="color:var(--text-muted)">Waiting for UDM monitor data…</td></tr>';
    return;
  }

  // Sort: cloudflare/1.1.1.1 first, then google, then rest
  const ordered = [...targets].sort((a, b) => {
    const rank = t => (t === '1.1.1.1' ? 0 : t === 'google.com' ? 1 : t === 'www.microsoft.com' ? 2 : 3);
    return rank(a) - rank(b) || a.localeCompare(b);
  });

  // Build rows
  const rows = ordered.map(target => {
    const tkey = target.replace(/\./g, '_');
    const cells = ['wan1', 'wan2'].map(w => {
      const lat = state[`udm.${w}.mon.${tkey}.latency_ms`];
      const avail = state[`udm.${w}.mon.${tkey}.availability`];
      const hasData = lat != null || avail != null;
      if (!hasData) return '<td class="lat-off">--</td>';
      const latStr = (lat != null && lat > 0) ? `${lat} ms` : '--';
      const cls = _latClass(lat);
      const availPill = (avail != null && avail < 100)
        ? `<span class="avail-pill">${Number(avail).toFixed(0)}%</span>`
        : '';
      return `<td class="${cls}">${latStr}${availPill}</td>`;
    });
    return `<tr><td>${_targetLabel(target)}</td>${cells.join('')}</tr>`;
  });
  tbody.innerHTML = rows.join('');
}

function _miniBars(level) {
  // 5-bar horizontal mini signal indicator; level 1-5 or 0 (no signal)
  const cls = level > 0 ? `band-bars lvl-${level}` : 'band-bars band-bars-empty';
  return `<span class="${cls}"><i></i><i></i><i></i><i></i><i></i></span>`;
}

function updateBandTable() {
  const tbody = document.getElementById('bands-tbody');
  const countEl = document.getElementById('bands-count');
  if (!tbody) return;
  const bands = state['br1.wan2.bands'];
  const n = Array.isArray(bands) ? bands.length : 0;
  if (countEl) countEl.textContent = n > 0 ? `${n} band${n === 1 ? '' : 's'}` : '—';
  if (n === 0) {
    tbody.innerHTML = '<tr><td colspan="3" style="color:var(--text-muted)">No band data</td></tr>';
    return;
  }
  tbody.innerHTML = '';
  for (const b of bands) {
    const tr = document.createElement('tr');
    tr.className = 'rat-' + (b.rat || '').toLowerCase();
    const shortName = (b.name || '').replace(/^(5G|LTE)\s+/, '');
    const rsrpLevel = signalLevelRSRP(b.rsrp);
    const sinrLevel = signalLevelSINR(b.sinr);
    tr.innerHTML = `
      <td>${shortName}</td>
      <td>${_miniBars(rsrpLevel)}</td>
      <td>${_miniBars(sinrLevel)}</td>
    `;
    tbody.appendChild(tr);
  }
}

// Tracks which carrier was most-recently requested so the UI can show it as
// "pending" (highlighted) until the modem actually re-registers.
let pendingCarrier = null;

async function switchCarrier(carrier, btn) {
  const labels = {verizon: 'Verizon', att: 'AT&T', tmobile: 'T-Mobile', auto: 'Automatic'};
  const label = labels[carrier] || carrier;
  if (!confirm(
    `Switch RoamLink eSIM to ${label}?\n\n` +
    `The cellular modem will briefly disconnect (~15-30 sec) then re-register.\n` +
    `If ${label} has no coverage here, it may fall back to another carrier.`
  )) return;

  // Immediately mark this carrier as pending so UI reflects the change
  pendingCarrier = carrier;
  document.querySelectorAll('.carrier-btn').forEach(b => {
    b.classList.toggle('pending', b.dataset.carrier === carrier);
  });

  btn.classList.add('working');
  document.querySelectorAll('.carrier-btn').forEach(b => { b.disabled = true; });
  try {
    const r = await authFetch('/api/control/br1/carrier', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({carrier}),
    });
    if (!r.ok) throw new Error(await r.text());
    btn.classList.remove('working');
    // Clear pending after 60s regardless — by then the modem should have reconnected
    // and the real state will take over
    setTimeout(() => {
      pendingCarrier = null;
      updateCellularStyling();
    }, 60000);
  } catch (e) {
    pendingCarrier = null;
    btn.classList.remove('working');
    alert('Carrier switch failed: ' + e.message);
  } finally {
    document.querySelectorAll('.carrier-btn').forEach(b => { b.disabled = false; });
  }
}

function installCarrierButtons() {
  document.querySelectorAll('.carrier-btn').forEach(btn => {
    if (btn.dataset.wired) return;
    btn.dataset.wired = '1';
    btn.addEventListener('click', () => switchCarrier(btn.dataset.carrier, btn));
  });
  document.querySelectorAll('.rat-btn').forEach(btn => {
    if (btn.dataset.wired) return;
    btn.dataset.wired = '1';
    btn.addEventListener('click', () => switchRat(btn.dataset.rat, btn));
  });
}

let pendingRat = null;

async function switchRat(mode, btn) {
  const labels = {auto: 'Auto (5G/LTE)', 'LTE': 'LTE only', '3G': '3G only'};
  const label = labels[mode] || mode;
  if (!confirm(
    `Lock cellular modem to: ${label}?\n\n` +
    `The modem will briefly disconnect and re-register.\n` +
    `"LTE only" disables 5G aggregation — useful when 5G signal is weak and dragging down throughput.`
  )) return;
  pendingRat = mode;
  document.querySelectorAll('.rat-btn').forEach(b => {
    b.classList.toggle('pending', b.dataset.rat === mode);
    b.disabled = true;
  });
  try {
    const r = await authFetch('/api/control/br1/rat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mode}),
    });
    if (!r.ok) throw new Error(await r.text());
    // Clear pending after 30s fallback — real state will take over once modem re-registers
    setTimeout(() => { pendingRat = null; updateRatButtons(); }, 30000);
  } catch (e) {
    pendingRat = null;
    alert('RAT lock failed: ' + e.message);
  } finally {
    document.querySelectorAll('.rat-btn').forEach(b => b.disabled = false);
  }
}

function updateRatButtons() {
  const activeRat = state['br1.wan2.rat_mode'];
  document.querySelectorAll('.rat-btn').forEach(b => {
    const isPending = pendingRat && b.dataset.rat === pendingRat;
    const isActive = !pendingRat && activeRat && b.dataset.rat === activeRat;
    b.classList.toggle('pending', Boolean(isPending));
    b.classList.toggle('active', Boolean(isActive));
  });
  // Clear pending once the modem's dataTechnology matches our request
  if (pendingRat && !pendingRat.includes(activeRat || '') && activeRat !== 'unknown') {
    // Wait for dataTechnology match
    if (pendingRat === activeRat) pendingRat = null;
  }
}

function updatePingColors() {
  for (const key of Object.keys(state)) {
    let m = key.match(/^ping\.(.+)\.latency_ms$/);
    if (m) {
      const tkey = m[1];
      const el = document.getElementById(`ping-lat-${tkey}`);
      if (el) _colorize(el, state[`ping.${tkey}.status`], state[key]);
      continue;
    }
    m = key.match(/^br1_internet\.(.+)\.latency_ms$/);
    if (m) {
      const tkey = m[1];
      const el = document.getElementById(`br1int-lat-${tkey}`);
      if (el) _colorize(el, state[`br1_internet.${tkey}.status`], state[key]);
    }
  }
}

// --- WebSocket Client ---
let connecting = false;
let lastConnectAt = 0;
let statusDebounceTimer = null;

function setStatus(text, cls) {
  // Debounce rapid status flipping so the UI doesn't twitch on brief reconnects
  clearTimeout(statusDebounceTimer);
  statusDebounceTimer = setTimeout(() => {
    const el = document.getElementById('ws-status');
    if (el) {
      el.textContent = text;
      el.className = 'badge ' + cls;
    }
  }, text === 'connected' ? 0 : 1500);
}

function connect() {
  // Prevent duplicate connects
  if (connecting) return;
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

  // Rate limit connect attempts to avoid a tight loop
  const now = Date.now();
  const sinceLast = now - lastConnectAt;
  if (sinceLast < 500) {
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 500 - sinceLast);
    return;
  }
  lastConnectAt = now;
  connecting = true;

  if (ws) {
    try { ws.onclose = null; ws.onerror = null; ws.close(); } catch {}
  }

  ws = new WebSocket(wsURL());

  ws.onopen = () => {
    connecting = false;
    reconnectDelay = RECONNECT_BASE;
    setStatus('connected', 'badge-connected');
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === 'full_state') {
        // Merge into existing state instead of replacing (prevents DOM rebuild flicker on reconnect)
        const incoming = msg.data || {};
        Object.assign(state, incoming);
        // Overwrite history only if we have more than what's already in memory
        if (msg.history) {
          for (const [k, v] of Object.entries(msg.history)) {
            if (!history[k] || v.length > history[k].length) history[k] = v;
          }
        }
        updateDOM(incoming);
      } else if (msg.type === 'update') {
        updateDOM(msg.data || {});
      }
      updatePingColors();
      updateCellularStyling();
      updateSpeedFusionRisk();
      updateWanLatencies();
      updateSfToggle();
      updateUdmMonitorTable();
      updateEventLog();
      updateUsageWidget();
      updateRatButtons();
    } catch (e) {
      console.error('Message parse error:', e);
    }
  };

  ws.onclose = (ev) => {
    connecting = false;
    // Code 1008 is what the server sends on token mismatch. Re-prompt
    // rather than backing off forever into "reconnecting…" hell.
    if (ev && ev.code === 1008) {
      setStatus('auth failed', 'badge-connecting');
      setToken('');
      promptForToken('Token was rejected by the server. Paste a valid NETMON_API_TOKEN:');
      reconnectDelay = RECONNECT_BASE;
      scheduleReconnect();
      return;
    }
    setStatus('reconnecting', 'badge-connecting');
    scheduleReconnect();
  };

  ws.onerror = () => {};
}

function scheduleReconnect() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX);
    connect();
  }, reconnectDelay);
}

// Reconnect when page becomes visible (iOS wakeup after screen sleep)
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
      reconnectDelay = RECONNECT_BASE;
      clearTimeout(reconnectTimer);
      connect();
    }
  }
});

// Keep-alive ping every 15s to prevent iOS from closing idle sockets
setInterval(() => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { ws.send('ping'); } catch {}
  }
}, 15000);

// --- WAN Control Buttons ---
const DRAIN_WAIT_SECONDS = 15;

async function wanControl(device, wanId, kind, body, btn) {
  // Graceful-disable path: the Disable button fires drain-and-disable
  // instead of the immediate-disable endpoint. The user sees a 15s
  // countdown + cancel button while the router demotes this WAN's
  // priority so in-flight flows migrate before the interface drops.
  // Enable / Prefer / Standby stay on their synchronous endpoints —
  // they either add capacity or change routing without the disruption.
  if (kind === 'disable') {
    if (!confirm(
      `Disable ${device.toUpperCase()} WAN${wanId}?\n\n` +
      `Priority will be demoted to Standby first, then the WAN will ` +
      `be disabled after ${DRAIN_WAIT_SECONDS}s. You can cancel mid-countdown.`
    )) return;
    await startGracefulDrain({
      device, wanId,
      url: `/api/devices/${device}/wan/${wanId}/drain-and-disable?wait=${DRAIN_WAIT_SECONDS}`,
      btn,
    });
    return;
  }

  const confirmMsg = {
    enable: `Enable ${device.toUpperCase()} WAN${wanId}?`,
    prefer: `Make ${device.toUpperCase()} WAN${wanId} the preferred (priority 1) WAN?`,
    standby: `Move ${device.toUpperCase()} WAN${wanId} to priority 2 (standby)?`,
  }[kind] || 'Apply change?';
  if (!confirm(confirmMsg)) return;

  btn.classList.add('working');
  btn.disabled = true;
  try {
    const url = `/api/control/${device}/wan/${wanId}/${kind === 'enable' ? 'enable' : 'priority'}`;
    const r = await authFetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await r.text());
    btn.classList.remove('working');
    btn.disabled = false;
    // brief feedback
    const orig = btn.textContent;
    btn.textContent = '✓';
    setTimeout(() => { btn.textContent = orig; }, 1500);
  } catch (e) {
    btn.classList.remove('working');
    btn.disabled = false;
    alert('Control failed: ' + e.message);
  }
}

// Kick off a graceful drain + disable. Returns immediately; the server
// publishes drain state via the WS stream, which our state updater
// picks up to render the countdown inside the Disable button.
async function startGracefulDrain({ device, wanId, url, btn }) {
  btn.classList.add('working');
  btn.disabled = true;
  try {
    const r = await authFetch(url, { method: 'POST' });
    if (!r.ok) throw new Error(await r.text());
  } catch (e) {
    btn.classList.remove('working');
    btn.disabled = false;
    alert('Graceful disable failed: ' + e.message);
  }
}

// Called once per updateDOM cycle to paint any active drains on the
// matching Disable buttons. A "draining" state key makes the button
// flip to "Cancel (12s)"; a second click posts the cancel endpoint.
function renderDrainCountdowns() {
  const now = Date.now() / 1000;
  const drainingKeys = Object.keys(state).filter(
    k => k.endsWith('.drain_status') && state[k] === 'draining'
  );
  // Reset any Disable buttons whose WAN is no longer draining.
  for (const btn of document.querySelectorAll('.btn-disable[data-drain-active="1"]')) {
    const expectedKey = btn.getAttribute('data-drain-key');
    if (!drainingKeys.includes(expectedKey)) {
      btn.textContent = 'Disable';
      btn.classList.remove('working', 'btn-cancel');
      btn.disabled = false;
      btn.removeAttribute('data-drain-active');
      btn.onclick = btn._origOnClick;
      btn._origOnClick = null;
    }
  }
  // Paint any newly-draining WANs.
  for (const key of drainingKeys) {
    const m = key.match(/^(.+)\.wan(\d+)\.drain_status$/);
    if (!m) continue;
    const [_, device, wan] = m;
    const wanId = parseInt(wan, 10);
    const endsAt = state[`${device}.wan${wanId}.drain_ends_at`] || 0;
    const remaining = Math.max(0, Math.ceil(endsAt - now));
    // Find the matching row's Disable button.
    const row = document.querySelector(
      `[data-ctrl-device="${device}"][data-ctrl-wan-id="${wanId}"]`
    );
    if (!row) continue;
    const btn = row.querySelector('.btn-disable');
    if (!btn) continue;
    if (btn.getAttribute('data-drain-active') !== '1') {
      btn._origOnClick = btn.onclick;
      btn.setAttribute('data-drain-active', '1');
      btn.setAttribute('data-drain-key', key);
      btn.classList.add('btn-cancel');
      btn.classList.remove('working');
      btn.disabled = false;
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          const r = await authFetch(
            `/api/devices/${device}/wan/${wanId}/drain`,
            { method: 'DELETE' }
          );
          if (!r.ok && r.status !== 404) {
            throw new Error(await r.text());
          }
        } catch (e) {
          alert('Cancel failed: ' + e.message);
          btn.disabled = false;
        }
      };
    }
    btn.textContent = `Cancel (${remaining}s)`;
  }
}

function buildWanControls() {
  // Supports both legacy <td.wan-controls> (table rows) and new inline <span.wan-controls> inside cards
  for (const cell of document.querySelectorAll('.wan-controls')) {
    if (cell.dataset.built) continue;
    const row = cell.closest('[data-ctrl-device]');
    if (!row) continue;
    const device = row.dataset.ctrlDevice;
    const wanId = parseInt(row.dataset.ctrlWanId, 10);
    const mode = row.dataset.ctrlMode || 'full';
    if (!device || !wanId) continue;

    // Enable/Disable buttons (shown in all modes except priority-only)
    if (mode !== 'priority-only') {
      const disableBtn = document.createElement('button');
      disableBtn.className = 'wan-btn btn-disable';
      disableBtn.textContent = 'Disable';
      disableBtn.title = mode === 'enable-only'
        ? 'Remove this WAN from the SpeedFusion bond'
        : 'Turn off this WAN connection';
      disableBtn.onclick = () => wanControl(device, wanId, 'disable', {enable: false}, disableBtn);

      const enableBtn = document.createElement('button');
      enableBtn.className = 'wan-btn btn-enable';
      enableBtn.textContent = 'Enable';
      enableBtn.onclick = () => wanControl(device, wanId, 'enable', {enable: true}, enableBtn);

      cell.appendChild(disableBtn);
      cell.appendChild(enableBtn);
    }

    // Prefer/Standby only make sense for priority-based failover, not bonded WANs
    if (mode !== 'enable-only') {
      const preferBtn = document.createElement('button');
      preferBtn.className = 'wan-btn btn-prefer';
      preferBtn.textContent = 'Prefer';
      preferBtn.title = 'Set priority 1 (primary)';
      preferBtn.onclick = () => wanControl(device, wanId, 'prefer', {priority: 1}, preferBtn);

      const standbyBtn = document.createElement('button');
      standbyBtn.className = 'wan-btn';
      standbyBtn.textContent = 'Standby';
      standbyBtn.title = 'Set priority 2 (backup)';
      standbyBtn.onclick = () => wanControl(device, wanId, 'standby', {priority: 2}, standbyBtn);

      cell.appendChild(preferBtn);
      cell.appendChild(standbyBtn);
    }
    cell.dataset.built = '1';
  }
}

// --- Widget Reordering (drag & drop with localStorage persistence) ---
const LAYOUT_KEY = 'netmon.layout.v2';
const CARDS_LAYOUT_KEY = 'netmon.cards.layout.v1';

function getSavedLayout(key) {
  try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch { return null; }
}

function saveLayout() {
  const main = document.getElementById('dashboard');
  if (main) {
    const rowOrder = [...main.querySelectorAll('section.row[data-row-id]')]
      .map(s => s.getAttribute('data-row-id')).filter(Boolean);
    localStorage.setItem(LAYOUT_KEY, JSON.stringify(rowOrder));
  }
  const devRow = document.getElementById('row-devices');
  if (devRow) {
    const cardOrder = [...devRow.querySelectorAll('.draggable-card[data-card-id]')]
      .map(c => c.getAttribute('data-card-id')).filter(Boolean);
    localStorage.setItem(CARDS_LAYOUT_KEY, JSON.stringify(cardOrder));
  }
}

function applySavedLayout() {
  const main = document.getElementById('dashboard');
  const rowOrder = getSavedLayout(LAYOUT_KEY);
  if (main && Array.isArray(rowOrder)) {
    for (const rowId of rowOrder) {
      const row = main.querySelector(`[data-row-id="${rowId}"]`);
      if (row) main.appendChild(row);
    }
  }
  const devRow = document.getElementById('row-devices');
  const cardOrder = getSavedLayout(CARDS_LAYOUT_KEY);
  if (devRow && Array.isArray(cardOrder)) {
    for (const cardId of cardOrder) {
      const card = devRow.querySelector(`[data-card-id="${cardId}"]`);
      if (card) devRow.appendChild(card);
    }
  }
}

function installDragHandles() {
  const addHandle = (container) => {
    const header = container.querySelector('.card-header');
    if (header && !header.querySelector('.drag-handle')) {
      const handle = document.createElement('span');
      handle.className = 'drag-handle';
      handle.textContent = '⋮⋮';
      handle.title = 'Hold and drag to reorder';
      header.insertBefore(handle, header.firstChild);
    }
  };
  document.querySelectorAll('section.row[data-row-id]').forEach(addHandle);
  document.querySelectorAll('.draggable-card').forEach(addHandle);
}

function _handleDrag(root, selector) {
  // Pointer-based drag: works on desktop, mobile, and tablets uniformly.
  let dragged = null;
  let startX = 0, startY = 0;
  let pointerId = null;
  let threshold = 6; // px before drag starts
  let activated = false;

  function getHandleFrom(e) {
    if (!document.body.classList.contains('edit-mode')) return null;
    // Only start drag when pressing on a drag handle or a card header
    const handle = e.target.closest('.drag-handle, .card-header');
    if (!handle) return null;
    const item = handle.closest(selector);
    if (!item || !root.contains(item)) return null;
    return item;
  }

  function getItemUnderPoint(x, y) {
    const candidates = [...root.querySelectorAll(selector)];
    for (const c of candidates) {
      const r = c.getBoundingClientRect();
      if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return c;
    }
    return null;
  }

  root.addEventListener('pointerdown', (e) => {
    const item = getHandleFrom(e);
    if (!item) return;
    // Stop the browser from starting text selection / native drag
    e.preventDefault();
    dragged = item;
    pointerId = e.pointerId;
    startX = e.clientX;
    startY = e.clientY;
    activated = false;
    try { e.target.setPointerCapture?.(e.pointerId); } catch {}
  });

  root.addEventListener('pointermove', (e) => {
    if (!dragged || e.pointerId !== pointerId) return;
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    if (!activated) {
      if (Math.hypot(dx, dy) < threshold) return;
      activated = true;
      dragged.classList.add('dragging');
    }
    e.preventDefault();

    // Highlight potential drop target
    dragged.style.pointerEvents = 'none';
    const over = getItemUnderPoint(e.clientX, e.clientY);
    dragged.style.pointerEvents = '';
    root.querySelectorAll(selector).forEach(r => r.classList.remove('drag-over'));
    if (over && over !== dragged && over.parentElement === dragged.parentElement) {
      over.classList.add('drag-over');
    }
  }, {passive: false});

  function finishDrag(e) {
    if (!dragged || (pointerId !== null && e.pointerId !== pointerId)) return;
    if (activated) {
      dragged.style.pointerEvents = 'none';
      const over = getItemUnderPoint(e.clientX, e.clientY);
      dragged.style.pointerEvents = '';
      if (over && over !== dragged && over.parentElement === dragged.parentElement) {
        const rect = over.getBoundingClientRect();
        const parentIsGrid = window.getComputedStyle(over.parentElement).display.includes('grid');
        const after = parentIsGrid
          ? (e.clientX - rect.left) > rect.width / 2
          : (e.clientY - rect.top) > rect.height / 2;
        if (after) over.after(dragged); else over.before(dragged);
        saveLayout();
      }
    }
    root.querySelectorAll(selector).forEach(r => r.classList.remove('dragging', 'drag-over'));
    dragged = null;
    pointerId = null;
    activated = false;
  }

  root.addEventListener('pointerup', finishDrag);
  root.addEventListener('pointercancel', finishDrag);
}

function initDragAndDrop() {
  const main = document.getElementById('dashboard');
  if (!main) return;
  _handleDrag(main, 'section.row[data-row-id]');
  const devRow = document.getElementById('row-devices');
  if (devRow) _handleDrag(devRow, '.draggable-card');
}

function setEditMode(on) {
  document.body.classList.toggle('edit-mode', on);
  const btn = document.getElementById('edit-toggle');
  if (btn) btn.textContent = on ? 'Done' : 'Edit';
}

document.addEventListener('DOMContentLoaded', () => {
  applySavedLayout();
  installDragHandles();
  initDragAndDrop();
  buildWanControls();
  installCarrierButtons();
  const btn = document.getElementById('edit-toggle');
  if (btn) {
    btn.addEventListener('click', () => {
      setEditMode(!document.body.classList.contains('edit-mode'));
    });
  }
});

// --- Init ---
connect();
