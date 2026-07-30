/* Month picker and Excel export for SN Dashboard */

let activeMonthKey = typeof CURRENT_MONTH_KEY !== 'undefined' ? CURRENT_MONTH_KEY : '';
let INLINE_MONTH_DATA = null;

function snapshotInlineMonth() {
  INLINE_MONTH_DATA = {
    monthly: JSON.parse(JSON.stringify(DATA)),
    weekly: JSON.parse(JSON.stringify(WEEKLY_DATA)),
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
      label: prev.label || formatMonthLabel(key),
    };
  }
  const p = DASHBOARD_HISTORY[key];
  if (!p) return null;
  return {
    monthly: p.monthly || [],
    weekly: p.weekly || { weeks: [], data: [] },
    label: p.label || formatMonthLabel(key),
  };
}

function applyMonth(key, renderNow) {
  const payload = monthPayload(key);
  if (!payload) return false;

  DATA = payload.monthly;
  WEEKLY_DATA = payload.weekly;

  const monthTitle = document.getElementById('monthTitle');
  if (monthTitle) monthTitle.textContent = payload.label;

  if (!renderNow) return true;

  render(DATA);
  renderWeeklyTable(WEEKLY_DATA);
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
  document.getElementById('exportBtn')?.addEventListener('click', exportExcel);
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

function exportExcel() {
  if (typeof XLSX === 'undefined') {
    alert('Excel 导出库未加载，请刷新页面重试');
    return;
  }

  const payload = DASHBOARD_HISTORY[activeMonthKey];
  if (!payload) {
    alert('没有可导出的数据');
    return;
  }

  const wb = XLSX.utils.book_new();

  const monthlyRows = (payload.monthly || []).map((r) => ({
    Employee: r.employee,
    Incidents: r.incidents,
    'SC Tasks': r.tasks,
    Total: r.incidents + r.tasks,
  }));
  monthlyRows.push({
    Employee: 'TOTAL',
    Incidents: monthlyRows.reduce((s, r) => s + r.Incidents, 0),
    'SC Tasks': monthlyRows.reduce((s, r) => s + r['SC Tasks'], 0),
    Total: monthlyRows.reduce((s, r) => s + r.Total, 0),
  });
  XLSX.utils.book_append_sheet(wb, XLSX.utils.json_to_sheet(monthlyRows), 'Monthly');

  const wd = payload.weekly || { weeks: [], data: [] };
  const weeklyRows = (wd.data || []).map((emp) => {
    const row = { Employee: emp.employee };
    (emp.weekly || []).forEach((cell, i) => {
      const c = cell.inc !== undefined ? cell : { inc: 0, task: 0 };
      row[`W${i + 1} INC`] = c.inc;
      row[`W${i + 1} Task`] = c.task;
      row[`W${i + 1} Total`] = c.inc + c.task;
    });
    const tot = emp.total || { inc: 0, task: 0 };
    row['Total INC'] = tot.inc;
    row['Total Task'] = tot.task;
    row['Grand Total'] = tot.inc + tot.task;
    return row;
  });
  if (weeklyRows.length) {
    XLSX.utils.book_append_sheet(wb, XLSX.utils.json_to_sheet(weeklyRows), 'Weekly');
  }

  const fname = `SN_Dashboard_${activeMonthKey.replace('-', '')}.xlsx`;
  XLSX.writeFile(wb, fname);
}

async function loadHistoryAndBoot() {
  snapshotInlineMonth();

  // Render inline HTML data immediately so the page does not stay at zeros
  // while dashboard_history.json is loading.
  if (typeof render === 'function' && INLINE_MONTH_DATA?.monthly?.length) {
    render(INLINE_MONTH_DATA.monthly);
    if (typeof renderWeeklyTable === 'function' && INLINE_MONTH_DATA.weekly) {
      renderWeeklyTable(INLINE_MONTH_DATA.weekly);
    }
  }

  try {
    const resp = await fetch('dashboard_history.json?' + Date.now());
    if (resp.ok) DASHBOARD_HISTORY = await resp.json();
  } catch (_) { /* fallback to inline DATA for current month */ }

  // Inline HTML is always fresher for the current month (updated each automation run).
  if (CURRENT_MONTH_KEY && INLINE_MONTH_DATA) {
    const prev = DASHBOARD_HISTORY[CURRENT_MONTH_KEY] || {};
    DASHBOARD_HISTORY[CURRENT_MONTH_KEY] = {
      ...prev,
      label: prev.label || formatMonthLabel(CURRENT_MONTH_KEY),
      monthly: INLINE_MONTH_DATA.monthly,
      weekly: INLINE_MONTH_DATA.weekly,
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
