/* Employee detail page — reads dashboard_history.json + inline current-month snapshot */

let DASHBOARD_HISTORY = {};

const HEATMAP_WD = ['日', '一', '二', '三', '四', '五', '六'];
const HEATMAP_TZ = 'Asia/Shanghai';
const HEATMAP_BLUE_SCALE = [
  '#e8f4ff', '#cfe8ff', '#b3d9ff', '#94c8ff', '#73b4ff',
  '#529eff', '#3b82f6', '#2f6fe8', '#245fd4', '#1d50bf',
  '#1744ab', '#133a96', '#0f3282', '#0c2b6e', '#09255c',
];
const DAY_COL_W = 36;
const WEEK_COL_W = 46;
const CHART_HEIGHT = 300;
const WEEK_BLOCK_GAP = 10;

function qp(name) {
  return new URLSearchParams(window.location.search).get(name);
}

function parseMonthKey(key) {
  const [y, m] = key.split('-').map(Number);
  return { y, m };
}

function daysInMonth(y, m) {
  return new Date(y, m, 0).getDate();
}

function dailyDateKey(y, m, d) {
  return `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
}

function isWeekend(y, m, d) {
  const wd = new Date(y, m - 1, d).getDay();
  return wd === 0 || wd === 6;
}

function cstTodayParts() {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-CA', {
      timeZone: HEATMAP_TZ,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour12: false,
    }).formatToParts(new Date())
      .filter((x) => x.type !== 'literal')
      .map((x) => [x.type, x.value])
  );
  return { y: +parts.year, m: +parts.month, d: +parts.day };
}

function formatMonthLabel(key) {
  const [y, m] = key.split('-').map(Number);
  return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'long' }).format(new Date(y, m - 1, 1));
}

function normWeekCell(v) {
  if (v && typeof v === 'object') return { inc: v.inc || 0, task: v.task || 0 };
  const n = +v || 0;
  return { inc: 0, task: n };
}

function weekColumnsForMonth(y, m, weeklyInfo) {
  const weeks = weeklyInfo?.weeks || [];
  const cols = [];
  weeks.forEach((label, wi) => {
    const endPart = label.split('-')[1];
    let endM;
    let endD;
    if (endPart.includes('/')) {
      [endM, endD] = endPart.split('/').map(Number);
    } else {
      endM = Number(label.split('/')[0]);
      endD = Number(endPart);
    }
    if (endM === m) {
      cols.push({
        afterDay: endD,
        weekIdx: wi,
        label,
        header: `W${wi + 1}`,
        isCurrent: wi === (weeklyInfo?.currentWeek ?? -1),
      });
    }
  });
  return cols.sort((a, b) => a.afterDay - b.afterDay);
}

function monthPayload(monthKey) {
  if (monthKey === CURRENT_MONTH_KEY && INLINE_MONTH_SNAPSHOT) {
    const prev = DASHBOARD_HISTORY[monthKey] || {};
    return {
      monthly: INLINE_MONTH_SNAPSHOT.monthly,
      weekly: INLINE_MONTH_SNAPSHOT.weekly,
      daily: INLINE_MONTH_SNAPSHOT.daily,
      label: prev.label || formatMonthLabel(monthKey),
    };
  }
  const p = DASHBOARD_HISTORY[monthKey];
  if (!p) return null;
  return {
    monthly: p.monthly || [],
    weekly: p.weekly || { weeks: [], data: [], currentWeek: -1 },
    daily: p.daily || null,
    label: p.label || formatMonthLabel(monthKey),
  };
}

function getEmployeeRecord(empName, monthKey) {
  const payload = monthPayload(monthKey);
  if (!payload) return null;

  const monthly = payload.monthly.find((r) => r.employee === empName);
  if (!monthly) return null;

  const { y, m } = parseMonthKey(monthKey);
  const matrix = payload.daily?.matrix?.[empName] || {};
  const daily = {};
  for (let d = 1; d <= daysInMonth(y, m); d++) {
    const key = dailyDateKey(y, m, d);
    daily[key] = matrix[key] || 0;
  }

  const weekCols = weekColumnsForMonth(y, m, payload.weekly);
  const row = (payload.weekly?.data || []).find((r) => r.employee === empName);
  const weekly = weekCols.map((col) => {
    const cell = row ? normWeekCell(row.weekly[col.weekIdx]) : { inc: 0, task: 0 };
    return { label: col.label, inc: cell.inc, task: cell.task };
  });

  return {
    incidents: monthly.incidents,
    tasks: monthly.tasks,
    daily,
    weekly,
    weekCols,
    currentWeek: payload.weekly?.currentWeek ?? -1,
  };
}

function futureAfterDay(monthKey) {
  const { y, m } = parseMonthKey(monthKey);
  const today = cstTodayParts();
  if (y === today.y && m === today.m) return today.d;
  return daysInMonth(y, m);
}

function buildDays(y, m, matrix, futureDayLimit) {
  const days = [];
  for (let d = 1; d <= daysInMonth(y, m); d++) {
    const key = dailyDateKey(y, m, d);
    days.push({
      key,
      day: d,
      weekend: isWeekend(y, m, d),
      wd: HEATMAP_WD[new Date(y, m - 1, d).getDay()],
      future: d > futureDayLimit,
      value: matrix[key] || 0,
    });
  }
  return days;
}

function dailyHeatColor(v) {
  if (v <= 0) return null;
  const level = Math.min(Math.max(Math.round(v), 1), 15);
  return HEATMAP_BLUE_SCALE[level - 1];
}

function buildWeekGroups(days, record) {
  const groups = [];
  const used = new Set();
  let prevAfter = 0;

  record.weekCols.forEach((col) => {
    const weekDays = days.filter((d) => d.day > prevAfter && d.day <= col.afterDay);
    weekDays.forEach((d) => used.add(d.day));
    const w = record.weekly.find((x) => x.label === col.label) || { inc: 0, task: 0 };
    groups.push({
      header: col.header,
      label: col.label,
      days: weekDays,
      week: { inc: w.inc, task: w.task },
      weekIdx: col.weekIdx,
      isCurrent: col.isCurrent,
      orphan: false,
    });
    prevAfter = col.afterDay;
  });

  const trailing = days.filter((d) => !used.has(d.day));
  if (trailing.length) {
    groups.push({
      header: '',
      label: trailing.length === 1
        ? `${trailing[0].day}日`
        : `${trailing[0].day}–${trailing[trailing.length - 1].day}日`,
      days: trailing,
      week: null,
      weekIdx: -1,
      isCurrent: false,
      orphan: true,
    });
  }
  return groups;
}

function flatDaysFromGroups(groups) {
  return groups.flatMap((g) => g.days);
}

function dayCellHtml(d) {
  const headCls = ['day-col', 'head'];
  if (d.weekend) headCls.push('weekend');
  const head = `<div class="${headCls.join(' ')}">${d.day}<span class="sub">${d.wd}</span></div>`;

  const valCls = ['day-col', 'val'];
  if (d.weekend) valCls.push('weekend');
  if (d.future) valCls.push('future');

  let innerCls = 'cell-inner';
  let innerStyle = '';
  let text;
  if (d.future) {
    text = '';
  } else if (d.weekend) {
    text = d.value > 0 ? String(d.value) : '·';
    const c = dailyHeatColor(d.value);
    if (c) innerStyle = ` style="background:${c};color:${d.value > 7 ? '#fff' : '#1e3a5f'}"`;
  } else if (d.value === 0) {
    text = '0';
    innerCls += ' blink-zero';
  } else {
    text = String(d.value);
    const c = dailyHeatColor(d.value);
    innerStyle = ` style="background:${c};color:${d.value > 7 ? '#fff' : '#1e3a5f'}"`;
  }
  const val = `<div class="${valCls.join(' ')}"><div class="${innerCls}"${innerStyle}>${text}</div></div>`;
  return { head, val };
}

function renderWeekBlock(group, mode) {
  const cls = ['week-block'];
  if (group.isCurrent) cls.push('current');
  if (group.orphan) cls.push('orphan');

  let heads = '';
  let vals = '';
  group.days.forEach((d) => {
    const cells = dayCellHtml(d);
    heads += cells.head;
    vals += cells.val;
  });

  let weekSumHead = '';
  let weekSumVal = '';
  if (!group.orphan && group.week) {
    const total = group.week.inc + group.week.task;
    weekSumHead = `<div class="week-sum-col head">${group.header}<span class="wk-h">${group.label}</span></div>`;
    weekSumVal = `<div class="week-sum-col" title="Inc ${group.week.inc} + Task ${group.week.task}">${total}</div>`;
  }

  const title = group.orphan
    ? `<div class="week-block-title">${group.label || '月初'}</div>`
    : `<div class="week-block-title">${group.header} · ${group.label}</div>`;

  if (mode === 'head') {
    return `<div class="${cls.join(' ')}">${title}<div class="week-block-row">${heads}${weekSumHead}</div></div>`;
  }
  return `<div class="${cls.join(' ')}"><div class="week-block-row">${vals}${weekSumVal}</div></div>`;
}

function renderChartMirror(groups) {
  return groups.map((g) => {
    const cls = ['week-block', 'chart-mirror'];
    if (g.orphan) cls.push('orphan');
    const pads = g.days.map(() => '<div class="day-col chart-pad"></div>').join('');
    const sumPad = !g.orphan ? '<div class="week-sum-col chart-pad"></div>' : '';
    return `<div class="${cls.join(' ')}"><div class="week-block-row">${pads}${sumPad}</div></div>`;
  }).join('');
}

let lineChart;

function weekBandsPlugin(layout) {
  return {
    id: 'weekBands',
    beforeDraw(chart) {
      const { ctx, chartArea, scales } = chart;
      if (!chartArea) return;
      const { top, bottom } = chartArea;
      const x = scales.x;
      let prev = chartArea.left;
      let band = 0;
      layout.weekBoundaries.forEach((boundaryX) => {
        const xMid = x.getPixelForValue(boundaryX);
        ctx.save();
        ctx.fillStyle = band % 2 === 0 ? 'rgba(241,245,249,0.65)' : 'rgba(226,232,240,0.5)';
        ctx.fillRect(prev, top, xMid - prev, bottom - top);
        ctx.restore();
        prev = xMid;
        band++;
      });
      ctx.save();
      ctx.fillStyle = band % 2 === 0 ? 'rgba(241,245,249,0.65)' : 'rgba(226,232,240,0.5)';
      ctx.fillRect(prev, top, chartArea.right - prev, bottom - top);
      ctx.restore();
    },
  };
}

function renderLineChart(flatDays, groups) {
  const canvas = document.getElementById('lineChart');
  const wrap = document.querySelector('.sync-chart-wrap');
  const headTrack = document.querySelectorAll('.sync-track')[0];
  const chartTrack = document.querySelector('.sync-track.chart-track');
  if (!canvas || !wrap || !headTrack || !chartTrack) return;

  const wrapRect = wrap.getBoundingClientRect();
  const dayCols = [...headTrack.querySelectorAll('.day-col.head')];
  const dayCenters = dayCols.map((col) => {
    const r = col.getBoundingClientRect();
    return r.left + r.width / 2 - wrapRect.left;
  });
  const plotWidth = chartTrack.getBoundingClientRect().width;
  const weekBoundaries = [...chartTrack.querySelectorAll('.week-block:not(.orphan) .week-sum-col')].map((col) => {
    const r = col.getBoundingClientRect();
    return r.left - wrapRect.left;
  });
  const layout = { dayCenters, totalWidth: plotWidth, weekBoundaries };

  wrap.style.width = `${plotWidth}px`;
  wrap.style.height = `${CHART_HEIGHT}px`;
  canvas.width = plotWidth;
  canvas.height = CHART_HEIGHT;
  canvas.style.width = `${plotWidth}px`;
  canvas.style.height = `${CHART_HEIGHT}px`;

  if (lineChart) lineChart.destroy();

  const points = flatDays.map((d, i) => ({
    x: dayCenters[i],
    y: d.future ? null : d.value,
  }));

  lineChart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      datasets: [{
        label: '每日关单',
        data: points,
        borderColor: '#2563eb',
        backgroundColor: 'rgba(37,99,235,0.12)',
        pointBackgroundColor: '#2563eb',
        pointRadius: 4,
        pointHoverRadius: 6,
        tension: 0.25,
        fill: true,
        spanGaps: false,
      }],
    },
    options: {
      responsive: false,
      maintainAspectRatio: false,
      devicePixelRatio: 1,
      parsing: false,
      layout: {
        autoPadding: false,
        padding: { left: 0, right: 0, top: 8, bottom: 4 },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items) => {
              const d = flatDays[items[0].dataIndex];
              return `${d.key} (${d.wd})`;
            },
            label: (ctx) => ` ${ctx.parsed.y ?? 0} 单`,
          },
        },
      },
      scales: {
        x: {
          type: 'linear',
          min: 0,
          max: plotWidth,
          display: false,
          offset: false,
          grid: { display: false },
        },
        y: {
          display: false,
          beginAtZero: true,
          ticks: { stepSize: 1 },
          grid: { color: 'rgba(148,163,184,0.2)' },
        },
      },
    },
    plugins: [weekBandsPlugin(layout)],
  });
}

function renderSyncPanel(days, record) {
  const groups = buildWeekGroups(days, record);
  const flatDays = flatDaysFromGroups(groups);
  const rowTotal = flatDays.reduce((s, d) => s + (d.future ? 0 : d.value), 0);

  const headTrack = groups.map((g) => renderWeekBlock(g, 'head')).join('');
  const valTrack = groups.map((g) => renderWeekBlock(g, 'val')).join('');
  const chartMirror = renderChartMirror(groups);

  document.getElementById('syncPanel').innerHTML = `
    <div class="sync-grid" style="--day-w:${DAY_COL_W}px;--week-w:${WEEK_COL_W}px;--block-gap:${WEEK_BLOCK_GAP}px;--chart-h:${CHART_HEIGHT}px">
      <div class="sync-label header">日期</div>
      <div class="sync-track">${headTrack}</div>
      <div class="sync-sum header">Σ</div>

      <div class="sync-label">每日</div>
      <div class="sync-track">${valTrack}</div>
      <div class="sync-sum">${rowTotal}</div>

      <div class="sync-chart-row">
        <div class="sync-label" style="align-self:start;padding-top:8px;">趋势</div>
        <div class="sync-chart-area">
          <div class="sync-track chart-track">${chartMirror}</div>
          <div class="sync-chart-wrap">
            <canvas id="lineChart"></canvas>
          </div>
        </div>
      </div>
    </div>`;

  requestAnimationFrame(() => renderLineChart(flatDays, groups));

  document.getElementById('weekLegend').innerHTML = groups
    .filter((g) => !g.orphan)
    .map((g, i) => `<span class="wk-chip" style="--band:${i % 2 ? '#e2e8f0' : '#f1f5f9'}">${g.header} ${g.label}</span>`)
    .join('');
}

function renderSummary(emp, record, monthLabel) {
  const total = record.incidents + record.tasks;
  const activeDays = Object.values(record.daily).filter((v) => v > 0).length;
  document.getElementById('empName').textContent = emp;
  document.getElementById('monthLabel').textContent = monthLabel;
  document.getElementById('sumInc').textContent = record.incidents;
  document.getElementById('sumTask').textContent = record.tasks;
  document.getElementById('sumAll').textContent = total;
  document.getElementById('sumDays').textContent = activeDays;
}

function renderWeekCards(record) {
  const wrap = document.getElementById('weekCards');
  wrap.innerHTML = record.weekCols.map((col) => {
    const w = record.weekly.find((x) => x.label === col.label) || { inc: 0, task: 0 };
    const total = w.inc + w.task;
    const cls = col.isCurrent ? 'week-card current' : 'week-card';
    return `<div class="${cls}">
      <div class="wk-title">${col.header}</div>
      <div class="wk-range">${col.label}</div>
      <div class="wk-stats"><span class="inc">${w.inc}</span> / <span class="task">${w.task}</span></div>
      <div class="wk-total">${total} 单</div>
    </div>`;
  }).join('');
}

function showError(msg) {
  const el = document.getElementById('errorBanner');
  el.style.display = 'block';
  el.textContent = msg;
}

function renderPage(emp, monthKey) {
  const payload = monthPayload(monthKey);
  if (!payload) {
    showError(`找不到 ${monthKey} 的数据，请从 Dashboard 重新进入或稍后再试。`);
    return;
  }

  const record = getEmployeeRecord(emp, monthKey);
  if (!record) {
    showError(`找不到员工「${emp}」在 ${formatMonthLabel(monthKey)} 的数据。`);
    return;
  }

  const { y, m } = parseMonthKey(monthKey);
  const days = buildDays(y, m, record.daily, futureAfterDay(monthKey));
  const monthLabel = payload.label || formatMonthLabel(monthKey);

  const back = document.getElementById('backLink');
  if (back) back.href = `dashboard.html?month=${encodeURIComponent(monthKey)}`;

  renderSummary(emp, record, monthLabel);
  renderWeekCards(record);
  renderSyncPanel(days, record);
}

async function boot() {
  const emp = decodeURIComponent(qp('employee') || '');
  const monthKey = qp('month') || CURRENT_MONTH_KEY;

  if (!emp) {
    showError('缺少员工参数。请从 Dashboard 点击人名进入。');
    return;
  }

  try {
    const resp = await fetch('dashboard_history.json?' + Date.now());
    if (resp.ok) DASHBOARD_HISTORY = await resp.json();
  } catch (_) { /* use inline snapshot only */ }

  if (CURRENT_MONTH_KEY && INLINE_MONTH_SNAPSHOT) {
    const prev = DASHBOARD_HISTORY[CURRENT_MONTH_KEY] || {};
    DASHBOARD_HISTORY[CURRENT_MONTH_KEY] = {
      ...prev,
      label: prev.label || formatMonthLabel(CURRENT_MONTH_KEY),
      monthly: INLINE_MONTH_SNAPSHOT.monthly,
      weekly: INLINE_MONTH_SNAPSHOT.weekly,
      daily: INLINE_MONTH_SNAPSHOT.daily,
    };
  }

  renderPage(emp, monthKey);
}

document.addEventListener('DOMContentLoaded', boot);
