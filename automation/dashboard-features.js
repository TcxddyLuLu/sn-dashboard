/* Month picker for SN Dashboard (charts only — Excel lives on tickets.html) */

let activeMonthKey = typeof CURRENT_MONTH_KEY !== 'undefined' ? CURRENT_MONTH_KEY : '';
let INLINE_MONTH_DATA = null;

function snapshotInlineMonth() {
  INLINE_MONTH_DATA = {
    monthly: JSON.parse(JSON.stringify(DATA)),
    weekly: JSON.parse(JSON.stringify(WEEKLY_DATA)),
    daily: typeof DAILY_DATA !== 'undefined' ? JSON.parse(JSON.stringify(DAILY_DATA)) : null,
  };
}

function formatMonthLabel(key) {
  const [y, m] = key.split('-').map(Number);
  const names = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
  ];
  return `${names[m - 1]} ${y}`;
}

function monthPayload(key) {
  if (key === CURRENT_MONTH_KEY && INLINE_MONTH_DATA) {
    const prev = DASHBOARD_HISTORY[key] || {};
    return {
      monthly: INLINE_MONTH_DATA.monthly,
      weekly: INLINE_MONTH_DATA.weekly,
      daily: INLINE_MONTH_DATA.daily,
      label: prev.label || formatMonthLabel(key),
    };
  }
  const p = DASHBOARD_HISTORY[key];
  if (!p) return null;
  return {
    monthly: p.monthly || [],
    weekly: p.weekly || { weeks: [], data: [] },
    daily: p.daily || null,
    label: p.label || formatMonthLabel(key),
  };
}

function clearDailyCache(monthKey) {
  if (typeof DAILY_DATA_CACHE === 'undefined') return;
  if (monthKey) delete DAILY_DATA_CACHE[monthKey];
  else Object.keys(DAILY_DATA_CACHE).forEach((k) => delete DAILY_DATA_CACHE[k]);
}

function applyMonth(key, renderNow) {
  const payload = monthPayload(key);
  if (!payload) return false;

  DATA = payload.monthly;
  WEEKLY_DATA = payload.weekly;
  if (payload.daily && key === CURRENT_MONTH_KEY && typeof DAILY_DATA !== 'undefined') {
    DAILY_DATA = payload.daily;
  }

  const monthTitle = document.getElementById('monthTitle');
  if (monthTitle) monthTitle.textContent = payload.label;

  if (!renderNow) return true;

  clearDailyCache(key);
  render(DATA);
  if (typeof renderDailyHeatmap === 'function') renderDailyHeatmap(key);
  return true;
}

function monthKeys() {
  const keys = Object.keys(DASHBOARD_HISTORY || {});
  return keys.sort().reverse();
}

function initToolbar() {
  const select = document.getElementById('monthSelect');
  if (!select) return;

  const keys = monthKeys();
  if (keys.length === 0 && activeMonthKey) keys.push(activeMonthKey);

  select.innerHTML = '';
  keys.forEach((key) => {
    const opt = document.createElement('option');
    opt.value = key;
    opt.textContent = (DASHBOARD_HISTORY[key] && DASHBOARD_HISTORY[key].label) || key;
    select.appendChild(opt);
  });

  if (!activeMonthKey && keys.length) activeMonthKey = keys[0];
  if (activeMonthKey) select.value = activeMonthKey;

  select.onchange = () => switchMonth(select.value);
  document.getElementById('monthPrev')?.addEventListener('click', () => shiftMonth(1));
  document.getElementById('monthNext')?.addEventListener('click', () => shiftMonth(-1));
  initRefreshButton();
}

const LOCAL_REFRESH_BASE = 'http://127.0.0.1:8090';
let refreshPollTimer = null;

function isLocalDashboardServer() {
  const host = window.location.hostname;
  const port = window.location.port;
  return port === '8090' || host === '127.0.0.1' || host === 'localhost';
}

function refreshApiUrl(path) {
  return isLocalDashboardServer()
    ? path
    : `${LOCAL_REFRESH_BASE}${path}`;
}

function setRefreshStatus(text) {
  const el = document.getElementById('refreshStatus');
  if (!el) return;
  if (text) {
    el.hidden = false;
    el.textContent = text;
  } else {
    el.hidden = true;
    el.textContent = '';
  }
}

function setRefreshBusy(busy) {
  const btn = document.getElementById('refreshBtn');
  if (btn) btn.disabled = busy;
}

function askRefreshPassword() {
  return window.prompt('请输入密码以手动更新 Dashboard：');
}

async function postRefresh(password) {
  return fetch(refreshApiUrl('/api/refresh'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  });
}

async function pollRefreshStatus() {
  try {
    const resp = await fetch(refreshApiUrl('/api/refresh?' + Date.now()));
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.running) {
      const sec = data.elapsed_sec || 0;
      setRefreshStatus(`正在查数并推送… ${sec}s（预计 2–3 分钟）`);
      return;
    }
    clearInterval(refreshPollTimer);
    refreshPollTimer = null;
    setRefreshBusy(false);
    if (data.exit_code === 0) {
      setRefreshStatus('已推送，等待 GitHub Pages…');
      setTimeout(() => window.location.reload(), 90000);
      setTimeout(() => setRefreshStatus('约 1 分钟后自动刷新页面'), 1000);
    } else {
      setRefreshStatus('更新失败，请查看本机 dashboard.log');
    }
  } catch (_) {
    /* keep polling */
  }
}

async function startManualRefresh() {
  if (!isLocalDashboardServer()) {
    const opened = window.open(LOCAL_REFRESH_BASE, '_blank');
    if (!opened) {
      setRefreshStatus('请打开 ' + LOCAL_REFRESH_BASE);
    }
    return;
  }

  const password = askRefreshPassword();
  if (password === null) return;

  setRefreshBusy(true);
  setRefreshStatus('正在启动…');
  try {
    const resp = await postRefresh(password);
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 401) {
      setRefreshStatus('密码错误');
      setRefreshBusy(false);
      return;
    }
    if (resp.status === 409) {
      setRefreshStatus('已有更新任务在跑');
    } else if (!resp.ok || !data.ok) {
      setRefreshStatus(data.error || '无法启动更新');
      setRefreshBusy(false);
      return;
    } else {
      setRefreshStatus('任务已启动…');
    }
    if (refreshPollTimer) clearInterval(refreshPollTimer);
    refreshPollTimer = setInterval(pollRefreshStatus, 3000);
    pollRefreshStatus();
  } catch (_) {
    setRefreshStatus('连接本地服务失败');
    setRefreshBusy(false);
  }
}

function initRefreshButton() {
  const btn = document.getElementById('refreshBtn');
  if (!btn) return;
  btn.addEventListener('click', startManualRefresh);

  const params = new URLSearchParams(window.location.search);
  if (params.get('refresh') === '1' && isLocalDashboardServer()) {
    params.delete('refresh');
    const qs = params.toString();
    const next = window.location.pathname + (qs ? `?${qs}` : '') + window.location.hash;
    window.history.replaceState({}, '', next);
    startManualRefresh();
  }
}

function shiftMonth(delta) {
  const keys = monthKeys();
  const idx = keys.indexOf(activeMonthKey);
  if (idx < 0) return;
  const next = idx + delta;
  if (next < 0 || next >= keys.length) return;
  switchMonth(keys[next]);
}

function switchMonth(key) {
  if (!key) return;
  activeMonthKey = key;
  const select = document.getElementById('monthSelect');
  if (select) select.value = key;
  applyMonth(key, true);
}

async function loadHistoryAndBoot() {
  snapshotInlineMonth();

  if (typeof render === 'function' && INLINE_MONTH_DATA?.monthly?.length) {
    render(INLINE_MONTH_DATA.monthly);
    if (typeof renderDailyHeatmap === 'function') {
      renderDailyHeatmap(activeMonthKey || CURRENT_MONTH_KEY);
    }
  }

  try {
    const resp = await fetch('dashboard_history.json?' + Date.now());
    if (resp.ok) DASHBOARD_HISTORY = await resp.json();
  } catch (_) { /* fallback to inline DATA for current month */ }

  if (CURRENT_MONTH_KEY && INLINE_MONTH_DATA) {
    const prev = DASHBOARD_HISTORY[CURRENT_MONTH_KEY] || {};
    DASHBOARD_HISTORY[CURRENT_MONTH_KEY] = {
      ...prev,
      label: prev.label || formatMonthLabel(CURRENT_MONTH_KEY),
      monthly: INLINE_MONTH_DATA.monthly,
      weekly: INLINE_MONTH_DATA.weekly,
      daily: INLINE_MONTH_DATA.daily || prev.daily,
    };
  }

  const keys = monthKeys();
  if (!activeMonthKey || (keys.length && !DASHBOARD_HISTORY[activeMonthKey])) {
    activeMonthKey = CURRENT_MONTH_KEY || keys[0] || '';
  } else if (!keys.length && CURRENT_MONTH_KEY) {
    activeMonthKey = CURRENT_MONTH_KEY;
  }

  applyMonth(activeMonthKey, false);

  initToolbar();
  if (typeof bootDashboard === 'function') bootDashboard();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', loadHistoryAndBoot);
} else {
  loadHistoryAndBoot();
}
