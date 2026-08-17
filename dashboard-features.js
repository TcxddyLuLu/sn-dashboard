/* Month picker and Excel export for SN Dashboard */

let activeMonthKey = typeof CURRENT_MONTH_KEY !== 'undefined' ? CURRENT_MONTH_KEY : '';
let INLINE_MONTH_DATA = null;
let DASHBOARD_TICKETS = {};

function monthlyFromTickets(tickets) {
  const totals = {};
  for (const t of tickets) {
    if (!totals[t.employee]) {
      totals[t.employee] = { employee: t.employee, incidents: 0, tasks: 0 };
    }
    if (t.type === 'Incident') totals[t.employee].incidents += 1;
    else totals[t.employee].tasks += 1;
  }
  return Object.values(totals).sort((a, b) => {
    const diff = (b.incidents + b.tasks) - (a.incidents + a.tasks);
    return diff !== 0 ? diff : a.employee.localeCompare(b.employee);
  });
}

function reconcileHistoryFromTickets() {
  for (const [monthKey, tickets] of Object.entries(DASHBOARD_TICKETS || {})) {
    if (!tickets?.length || !DASHBOARD_HISTORY[monthKey]) continue;
    DASHBOARD_HISTORY[monthKey].monthly = monthlyFromTickets(tickets);
  }
}

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

  const tickets = DASHBOARD_TICKETS[activeMonthKey] || payload.tickets || [];
  const monthlySource = tickets.length
    ? monthlyFromTickets(tickets)
    : (payload.monthly || []);

  const monthlyRows = monthlySource.map((r) => ({
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
      row[`W${i + 1} Total`] = c.inc + c.task;
    });
    const tot = emp.total || { inc: 0, task: 0 };
    row['Grand Total'] = tot.inc + tot.task;
    return row;
  });
  if (weeklyRows.length) {
    XLSX.utils.book_append_sheet(wb, XLSX.utils.json_to_sheet(weeklyRows), 'Weekly');
  }

  const daily = typeof getDailyData === 'function' ? getDailyData(activeMonthKey) : null;
  if (daily) {
    const dailyWide = daily.employees.map((emp) => {
      const row = { Employee: emp };
      let total = 0;
      daily.days.forEach((d) => {
        const v = daily.matrix[emp]?.[d.key] || 0;
        row[`${d.key} (${d.wd})`] = d.weekend ? '' : v;
        if (!d.weekend) total += v;
      });
      row.Total = total;
      return row;
    });
    XLSX.utils.book_append_sheet(wb, XLSX.utils.json_to_sheet(dailyWide), 'Daily Matrix');
  }

  if (tickets.length) {
    const ticketRows = tickets.map((t) => ({
      Employee: t.employee,
      Type: t.type,
      'Ticket #': t.number,
      'Closed Date': t.closed,
    }));
    XLSX.utils.book_append_sheet(wb, XLSX.utils.json_to_sheet(ticketRows), 'Tickets');
  }

  const fname = `SN_Dashboard_${activeMonthKey.replace('-', '')}.xlsx`;
  XLSX.writeFile(wb, fname);
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

  try {
    const ticketResp = await fetch('dashboard_tickets.json?' + Date.now());
    if (ticketResp.ok) DASHBOARD_TICKETS = await ticketResp.json();
  } catch (_) { /* ticket details optional for older months */ }

  reconcileHistoryFromTickets();

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

  reconcileHistoryFromTickets();

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
