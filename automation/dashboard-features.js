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
