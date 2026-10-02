/* Excel ticket export — separate page from main dashboard */

let DASHBOARD_HISTORY = {};
let DASHBOARD_TICKETS = {};
let activeMonthKey = '';

function formatMonthLabel(key) {
  const [y, m] = key.split('-').map(Number);
  const names = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
  ];
  return `${names[m - 1]} ${y}`;
}

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

function monthKeys() {
  const keys = new Set([
    ...Object.keys(DASHBOARD_HISTORY || {}),
    ...Object.keys(DASHBOARD_TICKETS || {}).filter((k) => !k.startsWith('_')),
  ]);
  return [...keys].sort().reverse();
}

function updateTicketsMeta() {
  const el = document.getElementById('ticketsUpdatedTime');
  const ts = DASHBOARD_TICKETS._excel_updated_at;
  if (el) {
    el.textContent = ts ? `最近更新：${ts}` : '最近更新：—';
  }
  const countEl = document.getElementById('ticketCount');
  if (countEl && activeMonthKey) {
    const n = (DASHBOARD_TICKETS[activeMonthKey] || []).length;
    countEl.textContent = n
      ? `当前月份明细：${n} 条工单`
      : '当前月份暂无明细数据（请稍后再试或联系 Luby）';
  }
}

function checkTicketsStale() {
  const banner = document.getElementById('staleBanner');
  if (!banner) return;
  const ts = DASHBOARD_TICKETS._excel_updated_at;
  if (!ts) {
    banner.style.display = 'block';
    banner.textContent =
      '⚠ 明细尚未成功更新过。Dashboard 图表与 Excel 明细分开更新，图表不受影响。';
    return;
  }
  const m = ts.match(/(\d{4})\/(\d{1,2})\/(\d{1,2})\s+(\d{1,2}):(\d{2})/);
  if (!m) return;
  const updated = new Date(
    Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5])
  );
  const hours = (Date.now() - updated.getTime()) / 3600000;
  if (hours > 12) {
    banner.style.display = 'block';
    banner.textContent = `⚠ 明细已超过 ${Math.round(hours)} 小时未更新，补跑任务会自动重试。`;
  }
}

function initMonthSelect() {
  const select = document.getElementById('monthSelect');
  if (!select) return;
  const keys = monthKeys();
  select.innerHTML = '';
  keys.forEach((key) => {
    const opt = document.createElement('option');
    opt.value = key;
    opt.textContent = (DASHBOARD_HISTORY[key] && DASHBOARD_HISTORY[key].label) || formatMonthLabel(key);
    select.appendChild(opt);
  });
  if (!activeMonthKey && keys.length) activeMonthKey = keys[0];
  if (activeMonthKey) select.value = activeMonthKey;
  select.onchange = () => {
    activeMonthKey = select.value;
    updateTicketsMeta();
  };
}

function buildDailyExport(monthKey) {
  const daily = DASHBOARD_HISTORY[monthKey] && DASHBOARD_HISTORY[monthKey].daily;
  if (!daily || !daily.matrix) return null;
  const employees = Object.keys(daily.matrix).sort();
  const dates = new Set();
  employees.forEach((emp) => {
    Object.keys(daily.matrix[emp] || {}).forEach((d) => dates.add(d));
  });
  const sortedDates = [...dates].sort();
  const rows = employees.map((emp) => {
    const row = { Employee: emp };
    let total = 0;
    sortedDates.forEach((d) => {
      const v = daily.matrix[emp][d] || 0;
      row[d] = v;
      total += v;
    });
    row.Total = total;
    return row;
  });
  return rows.length ? rows : null;
}

function exportExcel() {
  if (typeof XLSX === 'undefined') {
    alert('Excel 导出库未加载，请刷新页面重试');
    return;
  }
  const payload = DASHBOARD_HISTORY[activeMonthKey] || {};
  const ticketsRaw = DASHBOARD_TICKETS[activeMonthKey] || [];
  const tickets = Array.isArray(ticketsRaw) ? ticketsRaw : [];
  if (!tickets.length && !(payload.monthly || []).length) {
    alert('没有可导出的数据');
    return;
  }

  const wb = XLSX.utils.book_new();
  const monthlySource = tickets.length
    ? monthlyFromTickets(tickets)
    : (payload.monthly || []).map((r) => ({
        employee: r.employee,
        incidents: r.incidents,
        tasks: r.tasks,
      }));

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

  const dailyRows = buildDailyExport(activeMonthKey);
  if (dailyRows) {
    XLSX.utils.book_append_sheet(wb, XLSX.utils.json_to_sheet(dailyRows), 'Daily Matrix');
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

  XLSX.writeFile(wb, `SN_Dashboard_${activeMonthKey.replace('-', '')}.xlsx`);
}

async function bootTicketsPage() {
  try {
    const [histResp, ticketResp] = await Promise.all([
      fetch('dashboard_history.json?' + Date.now()),
      fetch('dashboard_tickets.json?' + Date.now()),
    ]);
    if (histResp.ok) DASHBOARD_HISTORY = await histResp.json();
    if (ticketResp.ok) DASHBOARD_TICKETS = await ticketResp.json();
  } catch (_) { /* show empty state */ }

  const keys = monthKeys();
  activeMonthKey = keys[0] || new Date().toISOString().slice(0, 7);

  initMonthSelect();
  updateTicketsMeta();
  checkTicketsStale();
  document.getElementById('exportBtn')?.addEventListener('click', exportExcel);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootTicketsPage);
} else {
  bootTicketsPage();
}
