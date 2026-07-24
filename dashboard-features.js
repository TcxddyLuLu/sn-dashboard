/* Month picker, category tab, Excel export for SN Dashboard */

let activeMonthKey = typeof CURRENT_MONTH_KEY !== 'undefined' ? CURRENT_MONTH_KEY : '';
let activeTab = 'volume';
let categoryCharts = [];

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

  document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
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

  const payload = DASHBOARD_HISTORY[key];
  if (!payload) return;

  DATA = payload.monthly || [];
  WEEKLY_DATA = payload.weekly || { weeks: [], data: [] };
  CATEGORY_DATA = payload.category || { axes: [], employees: [], colors: {} };

  const label = payload.label || key;
  const monthTitle = document.getElementById('monthTitle');
  if (monthTitle) monthTitle.textContent = label;
  const catTitle = document.getElementById('catMonthTitle');
  if (catTitle) catTitle.textContent = label;

  if (activeTab === 'volume') {
    render(DATA);
    renderWeeklyTable(WEEKLY_DATA);
  } else {
    renderCategory(CATEGORY_DATA);
  }
}

function switchTab(tab) {
  activeTab = tab;
  document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.tab === tab);
  });
  const vol = document.getElementById('tabVolume');
  const cat = document.getElementById('tabCategory');
  if (vol) vol.style.display = tab === 'volume' ? '' : 'none';
  if (cat) cat.style.display = tab === 'category' ? '' : 'none';

  if (tab === 'category') {
    renderCategory(CATEGORY_DATA);
  } else if (window.__lastChartData && typeof Chart !== 'undefined') {
    retryChart();
  }
}

function renderCategory(D) {
  if (!D || !D.axes || !D.employees) return;

  const axes = D.axes;
  const emps = D.employees.filter((e) => e.total > 0);
  const colors = D.colors || {};
  const fallback = '#94a3b8';

  const totalTickets = emps.reduce((s, e) => s + e.total, 0);
  document.getElementById('catTotalVal').textContent = totalTickets;
  document.getElementById('catAxesVal').textContent = axes.length;
  document.getElementById('catEmpVal').textContent = emps.length;

  const teamCounts = axes.map((_, ai) =>
    emps.reduce((s, e) => s + (e.counts[ai] || 0), 0)
  );
  const teamMax = Math.max(...teamCounts, 1);

  const barsEl = document.getElementById('catTeamBars');
  barsEl.innerHTML = '';
  axes
    .map((cat, i) => ({ cat, count: teamCounts[i] }))
    .sort((a, b) => b.count - a.count)
    .forEach((item) => {
      const pct = (item.count / teamMax) * 100;
      const row = document.createElement('div');
      row.className = 'cat-bar-row';
      row.innerHTML =
        `<span class="cat-dot" style="background:${colors[item.cat] || fallback}"></span>` +
        `<span class="cat-name">${item.cat}</span>` +
        `<div class="cat-bar-bg"><div class="cat-bar-fill" style="width:${pct}%;background:${colors[item.cat] || fallback}"></div></div>` +
        `<span class="cat-count">${item.count}</span>`;
      barsEl.appendChild(row);
    });

  let html = '<thead><tr><th>Employee</th><th>Total</th>';
  axes.forEach((a) => { html += `<th>${a}</th>`; });
  html += '</tr></thead><tbody>';

  emps.forEach((emp) => {
    html += `<tr><td>${emp.name}</td><td class="cat-total">${emp.total}</td>`;
    emp.counts.forEach((c) => {
      html += `<td>${c || ''}</td>`;
    });
    html += '</tr>';
  });
  html += '</tbody>';
  document.getElementById('catTable').innerHTML = html;

  categoryCharts.forEach((c) => c.destroy());
  categoryCharts = [];

  const canvas = document.getElementById('catTeamRadar');
  if (!canvas || typeof Chart === 'undefined') return;

  const teamPcts = teamCounts.map((c) =>
    totalTickets > 0 ? Math.round((c / totalTickets) * 100) : 0
  );
  categoryCharts.push(new Chart(canvas.getContext('2d'), {
    type: 'radar',
    data: {
      labels: axes,
      datasets: [{
        data: teamPcts,
        borderColor: 'rgba(37, 99, 235, 0.85)',
        backgroundColor: 'rgba(37, 99, 235, 0.12)',
        borderWidth: 2,
        pointRadius: 3,
      }],
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: {
        r: {
          beginAtZero: true,
          ticks: { display: false },
          grid: { color: '#e2e8f0' },
          pointLabels: { font: { size: 10 }, color: '#64748b' },
        },
      },
    },
  }));
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
  const label = payload.label || activeMonthKey;

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

  const cat = payload.category || { axes: [], employees: [] };
  if (cat.axes && cat.axes.length) {
    const catRows = (cat.employees || []).map((emp) => {
      const row = { Employee: emp.name, Total: emp.total };
      cat.axes.forEach((axis, i) => { row[axis] = emp.counts[i] || 0; });
      return row;
    });
    XLSX.utils.book_append_sheet(wb, XLSX.utils.json_to_sheet(catRows), 'Category');
  }

  const fname = `SN_Dashboard_${activeMonthKey.replace('-', '')}.xlsx`;
  XLSX.writeFile(wb, fname);
}

async function loadHistoryAndBoot() {
  try {
    const resp = await fetch('dashboard_history.json?' + Date.now());
    if (resp.ok) DASHBOARD_HISTORY = await resp.json();
  } catch (_) { /* fallback to inline DATA for current month */ }

  const keys = monthKeys();
  if (keys.length) {
    if (!activeMonthKey || !DASHBOARD_HISTORY[activeMonthKey]) {
      activeMonthKey = keys[0];
    }
    const p = DASHBOARD_HISTORY[activeMonthKey];
    if (p) {
      DATA = p.monthly || DATA;
      WEEKLY_DATA = p.weekly || WEEKLY_DATA;
      CATEGORY_DATA = p.category || CATEGORY_DATA;
      if (p.label) {
        const monthTitle = document.getElementById('monthTitle');
        if (monthTitle) monthTitle.textContent = p.label;
      }
    }
  } else if (CURRENT_MONTH_KEY) {
    activeMonthKey = CURRENT_MONTH_KEY;
  }

  initToolbar();
  if (typeof bootDashboard === 'function') bootDashboard();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', loadHistoryAndBoot);
} else {
  loadHistoryAndBoot();
}
