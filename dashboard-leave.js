/* Heatmap leave registry: manual PTO marks show as 休 instead of blinking 0 */

const LEAVE_STORAGE_KEY = 'sn_dashboard_leave_v1';
const LEAVE_UI_VERSION = '3';

/** @type {{ days: Record<string, string[]> }} */
let LEAVE_REGISTRY = { days: {} };

/** @type {Set<string>} */
let leavePickerSelected = new Set();
let leavePickerYear = 0;
let leavePickerMonth = 0;

function normalizeLeavePayload(raw) {
  const days = {};
  if (!raw || typeof raw !== 'object') return { days };
  if (raw.days && typeof raw.days === 'object' && !Array.isArray(raw.days)) {
    Object.entries(raw.days).forEach(([dateKey, emps]) => {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(dateKey) || !Array.isArray(emps)) return;
      const list = [...new Set(emps.map((e) => String(e).trim()).filter(Boolean))].sort();
      if (list.length) days[dateKey] = list;
    });
    return { days };
  }
  if (Array.isArray(raw.entries)) {
    raw.entries.forEach((row) => {
      const dateKey = String(row?.date || '').trim();
      const emp = String(row?.employee || '').trim();
      if (!dateKey || !emp) return;
      if (!days[dateKey]) days[dateKey] = [];
      if (!days[dateKey].includes(emp)) days[dateKey].push(emp);
    });
    Object.keys(days).forEach((k) => days[k].sort());
    return { days };
  }
  return { days };
}

function mergeLeaveDays(base, extra) {
  const days = { ...base.days };
  Object.entries(extra.days || {}).forEach(([dateKey, emps]) => {
    const set = new Set(days[dateKey] || []);
    (emps || []).forEach((e) => set.add(e));
    const list = [...set].sort();
    if (list.length) days[dateKey] = list;
    else delete days[dateKey];
  });
  return { days };
}

function loadLeaveFromLocalStorage() {
  try {
    const raw = localStorage.getItem(LEAVE_STORAGE_KEY);
    if (!raw) return { days: {} };
    return normalizeLeavePayload(JSON.parse(raw));
  } catch (_) {
    return { days: {} };
  }
}

function persistLeaveLocal() {
  try {
    localStorage.setItem(
      LEAVE_STORAGE_KEY,
      JSON.stringify({ days: LEAVE_REGISTRY.days, updated: new Date().toISOString() }),
    );
  } catch (_) { /* quota */ }
}

function isHeatmapLeaveDay(employee, dateKey) {
  if (!employee || !dateKey) return false;
  const list = LEAVE_REGISTRY.days[dateKey];
  return Array.isArray(list) && list.includes(employee);
}

function allDashboardEmployees() {
  const set = new Set();
  if (typeof DATA !== 'undefined' && Array.isArray(DATA)) {
    DATA.forEach((r) => r.employee && set.add(r.employee));
  }
  Object.values(typeof DASHBOARD_HISTORY !== 'undefined' ? DASHBOARD_HISTORY : {}).forEach((m) => {
    (m.monthly || []).forEach((r) => r.employee && set.add(r.employee));
    const matrix = m.daily?.matrix;
    if (matrix) Object.keys(matrix).forEach((e) => set.add(e));
  });
  return [...set].sort((a, b) => a.localeCompare(b));
}

function leaveEntriesSorted() {
  const rows = [];
  Object.entries(LEAVE_REGISTRY.days).forEach(([dateKey, emps]) => {
    emps.forEach((emp) => rows.push({ dateKey, emp }));
  });
  rows.sort((a, b) => b.dateKey.localeCompare(a.dateKey) || a.emp.localeCompare(b.emp));
  return rows;
}

function refreshHeatmapAfterLeaveChange() {
  const key =
    typeof activeMonthKey !== 'undefined' && activeMonthKey
      ? activeMonthKey
      : typeof CURRENT_MONTH_KEY !== 'undefined'
        ? CURRENT_MONTH_KEY
        : '';
  if (typeof renderDailyHeatmap === 'function' && key) renderDailyHeatmap(key);
}

function addLeaveEntry(employee, dateKey, options = {}) {
  const emp = String(employee || '').trim();
  const d = String(dateKey || '').trim();
  if (!emp || !/^\d{4}-\d{2}-\d{2}$/.test(d)) return false;
  if (!LEAVE_REGISTRY.days[d]) LEAVE_REGISTRY.days[d] = [];
  if (LEAVE_REGISTRY.days[d].includes(emp)) return true;
  LEAVE_REGISTRY.days[d].push(emp);
  LEAVE_REGISTRY.days[d].sort();
  if (!options.deferRefresh) {
    persistLeaveLocal();
    refreshHeatmapAfterLeaveChange();
  }
  return true;
}

function addLeaveEntries(employee, dateKeys) {
  const emp = String(employee || '').trim();
  if (!emp) return 0;
  let added = 0;
  dateKeys.forEach((dateKey) => {
    if (addLeaveEntry(emp, dateKey, { deferRefresh: true })) added += 1;
  });
  persistLeaveLocal();
  refreshHeatmapAfterLeaveChange();
  return added;
}

function removeLeaveEntry(employee, dateKey) {
  const list = LEAVE_REGISTRY.days[dateKey];
  if (!list) return;
  LEAVE_REGISTRY.days[dateKey] = list.filter((e) => e !== employee);
  if (!LEAVE_REGISTRY.days[dateKey].length) delete LEAVE_REGISTRY.days[dateKey];
  persistLeaveLocal();
  refreshHeatmapAfterLeaveChange();
}

function leaveApiUrl(path) {
  const host = window.location.hostname;
  const port = window.location.port;
  const onLocal =
    port === '8090' || host === '127.0.0.1' || host === 'localhost';
  const base = onLocal ? '' : 'http://127.0.0.1:8090';
  return `${base}${path}`;
}

async function fetchSharedLeaveDays() {
  try {
    const resp = await fetch('leave_days.json?' + Date.now());
    if (!resp.ok) return { days: {} };
    return normalizeLeavePayload(await resp.json());
  } catch (_) {
    return { days: {} };
  }
}

async function initLeaveDays() {
  const shared = await fetchSharedLeaveDays();
  const local = loadLeaveFromLocalStorage();
  LEAVE_REGISTRY = mergeLeaveDays(shared, local);
  persistLeaveLocal();
}

async function saveLeaveToServer() {
  const url = leaveApiUrl('/api/leave-days');
  const body = JSON.stringify({
    days: LEAVE_REGISTRY.days,
    updated: new Date().toISOString(),
  });
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function exportLeaveJson() {
  const blob = new Blob(
    [JSON.stringify({ days: LEAVE_REGISTRY.days, updated: new Date().toISOString() }, null, 2)],
    { type: 'application/json' },
  );
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'leave_days.json';
  a.click();
  URL.revokeObjectURL(a.href);
}

function importLeaveJsonFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const parsed = normalizeLeavePayload(JSON.parse(String(reader.result || '{}')));
        LEAVE_REGISTRY = mergeLeaveDays(LEAVE_REGISTRY, parsed);
        persistLeaveLocal();
        refreshHeatmapAfterLeaveChange();
        resolve();
      } catch (e) {
        reject(e);
      }
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsText(file);
  });
}

function leavePickerInitMonth() {
  if (typeof cstTodayParts === 'function') {
    const t = cstTodayParts();
    leavePickerYear = t.y;
    leavePickerMonth = t.m;
    return;
  }
  const now = new Date();
  leavePickerYear = now.getFullYear();
  leavePickerMonth = now.getMonth() + 1;
}

function leavePickerDateKey(y, m, d) {
  return `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
}

function leavePickerCurrentEmployee() {
  return String(document.getElementById('leaveEmpInput')?.value || '').trim();
}

function renderLeaveCalendar() {
  const grid = document.getElementById('leaveCalGrid');
  const title = document.getElementById('leaveCalTitle');
  const summary = document.getElementById('leaveCalSummary');
  if (!grid || !title) return;

  const emp = leavePickerCurrentEmployee();
  const y = leavePickerYear;
  const m = leavePickerMonth;
  title.textContent = `${y}年${m}月`;

  const firstDow = new Date(y, m - 1, 1).getDay();
  const daysInMonth = new Date(y, m, 0).getDate();
  const dows = ['日', '一', '二', '三', '四', '五', '六'];
  let html = dows.map((d) => `<div class="leave-cal-dow">${d}</div>`).join('');

  for (let i = 0; i < firstDow; i++) {
    html += '<div class="leave-cal-day muted" aria-hidden="true"></div>';
  }

  for (let d = 1; d <= daysInMonth; d++) {
    const key = leavePickerDateKey(y, m, d);
    const wd = new Date(y, m - 1, d).getDay();
    const weekend = wd === 0 || wd === 6;
    const classes = ['leave-cal-day'];
    if (weekend) classes.push('weekend');
    if (leavePickerSelected.has(key)) classes.push('selected');
    if (emp && isHeatmapLeaveDay(emp, key)) classes.push('registered');
    html += `<button type="button" class="${classes.join(' ')}" data-leave-day="${key}">${d}</button>`;
  }

  grid.innerHTML = html;
  grid.querySelectorAll('button[data-leave-day]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const key = btn.getAttribute('data-leave-day');
      if (!key) return;
      if (leavePickerSelected.has(key)) leavePickerSelected.delete(key);
      else leavePickerSelected.add(key);
      renderLeaveCalendar();
    });
  });

  if (summary) {
    const n = leavePickerSelected.size;
    summary.textContent = n
      ? `已选 ${n} 天（可点多天，再点一次取消）`
      : '在日历上点击选择多天';
  }
}

function ensureLeaveModal() {
  const existing = document.getElementById('leaveModal');
  if (existing?.dataset.leaveUi === LEAVE_UI_VERSION) return;
  existing?.remove();

  const wrap = document.createElement('div');
  wrap.id = 'leaveModal';
  wrap.className = 'leave-modal';
  wrap.dataset.leaveUi = LEAVE_UI_VERSION;
  wrap.hidden = true;
  wrap.innerHTML = `
    <div class="leave-modal-backdrop" data-leave-close></div>
    <div class="leave-modal-panel" role="dialog" aria-labelledby="leaveModalTitle">
      <header class="leave-modal-header">
        <h4 id="leaveModalTitle">休假登记</h4>
        <button type="button" class="leave-modal-close" data-leave-close aria-label="关闭">×</button>
      </header>
      <p class="leave-modal-hint">
        先选员工，再在下方日历<strong>点多天</strong>，一次提交。Heatmap 工作日 0 单显示「休」。
        浏览器会本地保存；本机 <code>8090</code> 可「保存到团队」。
      </p>
      <div class="leave-form">
        <label>员工
          <input type="text" id="leaveEmpInput" list="leaveEmpList" placeholder="选择或输入姓名" />
        </label>
        <datalist id="leaveEmpList"></datalist>
        <div class="leave-cal-wrap">
          <div class="leave-cal-head">
            <button type="button" id="leaveCalPrev" title="上个月">‹</button>
            <span class="leave-cal-title" id="leaveCalTitle"></span>
            <button type="button" id="leaveCalNext" title="下个月">›</button>
          </div>
          <div class="leave-cal-grid" id="leaveCalGrid"></div>
          <div class="leave-cal-actions">
            <span class="leave-cal-summary" id="leaveCalSummary"></span>
            <button type="button" class="btn-leave-secondary" id="leaveCalClear">清空选择</button>
            <button type="button" class="btn-leave-add" id="leaveAddBtn">添加所选日期</button>
          </div>
        </div>
      </div>
      <div class="leave-list-wrap">
        <div class="leave-list-head">已登记 <span id="leaveCount"></span></div>
        <ul class="leave-list" id="leaveList"></ul>
      </div>
      <footer class="leave-modal-footer">
        <button type="button" class="btn-leave-secondary" id="leaveExportBtn">导出 JSON</button>
        <label class="btn-leave-secondary leave-import-label">
          导入 JSON
          <input type="file" id="leaveImportInput" accept="application/json,.json" hidden />
        </label>
        <button type="button" class="btn-leave-primary" id="leaveServerBtn">保存到团队（本机 8090）</button>
      </footer>
      <p class="leave-status" id="leaveStatus" hidden></p>
    </div>
  `;
  document.body.appendChild(wrap);

  wrap.querySelectorAll('[data-leave-close]').forEach((el) => {
    el.addEventListener('click', () => {
      wrap.hidden = true;
    });
  });

  document.getElementById('leaveEmpInput')?.addEventListener('change', () => renderLeaveCalendar());
  document.getElementById('leaveEmpInput')?.addEventListener('input', () => renderLeaveCalendar());

  document.getElementById('leaveCalPrev')?.addEventListener('click', () => {
    leavePickerMonth -= 1;
    if (leavePickerMonth < 1) {
      leavePickerMonth = 12;
      leavePickerYear -= 1;
    }
    renderLeaveCalendar();
  });

  document.getElementById('leaveCalNext')?.addEventListener('click', () => {
    leavePickerMonth += 1;
    if (leavePickerMonth > 12) {
      leavePickerMonth = 1;
      leavePickerYear += 1;
    }
    renderLeaveCalendar();
  });

  document.getElementById('leaveCalClear')?.addEventListener('click', () => {
    leavePickerSelected.clear();
    renderLeaveCalendar();
  });

  document.getElementById('leaveAddBtn')?.addEventListener('click', () => {
    const emp = leavePickerCurrentEmployee();
    if (!emp) {
      setLeaveStatus('请先选择员工。', true);
      return;
    }
    const dates = [...leavePickerSelected].sort();
    if (!dates.length) {
      setLeaveStatus('请在日历上选择至少一天。', true);
      return;
    }
    const added = addLeaveEntries(emp, dates);
    leavePickerSelected.clear();
    renderLeaveCalendar();
    renderLeaveList();
    setLeaveStatus(`已为 ${emp} 登记 ${added} 天休假。`);
  });

  document.getElementById('leaveExportBtn')?.addEventListener('click', () => exportLeaveJson());

  document.getElementById('leaveImportInput')?.addEventListener('change', async (ev) => {
    const file = ev.target.files?.[0];
    ev.target.value = '';
    if (!file) return;
    try {
      await importLeaveJsonFile(file);
      renderLeaveList();
      renderLeaveCalendar();
      setLeaveStatus('已合并导入的记录。');
    } catch (_) {
      setLeaveStatus('导入失败：不是有效的 leave_days.json', true);
    }
  });

  document.getElementById('leaveServerBtn')?.addEventListener('click', async () => {
    setLeaveStatus('正在保存…');
    try {
      await saveLeaveToServer();
      setLeaveStatus('已写入 leave_days.json（下次 Dashboard 发布后会同步到 GitHub Pages）。');
    } catch (err) {
      setLeaveStatus(
        '保存失败：请在本机启动 dashboard 服务（8090），或改用导出 JSON 后放进项目再发布。',
        true,
      );
      console.warn(err);
    }
  });
}

function setLeaveStatus(msg, isError) {
  const el = document.getElementById('leaveStatus');
  if (!el) return;
  if (!msg) {
    el.hidden = true;
    el.textContent = '';
    return;
  }
  el.hidden = false;
  el.textContent = msg;
  el.classList.toggle('leave-status-error', !!isError);
}

function renderLeaveList() {
  const list = document.getElementById('leaveList');
  const count = document.getElementById('leaveCount');
  const datalist = document.getElementById('leaveEmpList');
  if (!list) return;

  const employees = allDashboardEmployees();
  if (datalist) {
    datalist.innerHTML = employees.map((e) => `<option value="${e.replace(/"/g, '&quot;')}">`).join('');
  }

  const rows = leaveEntriesSorted();
  if (count) count.textContent = `(${rows.length})`;
  if (!rows.length) {
    list.innerHTML = '<li class="leave-empty">暂无登记</li>';
    return;
  }
  list.innerHTML = rows
    .map(
      (r) => `<li>
        <span>${r.dateKey}</span>
        <span>${r.emp}</span>
        <button type="button" data-leave-del="${r.dateKey}" data-leave-emp="${encodeURIComponent(r.emp)}">删除</button>
      </li>`,
    )
    .join('');

  list.querySelectorAll('button[data-leave-del]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const dateKey = btn.getAttribute('data-leave-del');
      const emp = decodeURIComponent(btn.getAttribute('data-leave-emp') || '');
      removeLeaveEntry(emp, dateKey);
      renderLeaveList();
      renderLeaveCalendar();
    });
  });
}

function openLeaveModal() {
  ensureLeaveModal();
  const modal = document.getElementById('leaveModal');
  if (!modal) return;
  leavePickerSelected.clear();
  leavePickerInitMonth();
  renderLeaveList();
  renderLeaveCalendar();
  setLeaveStatus('');
  modal.hidden = false;
}

function bindLeaveButton() {
  document.querySelectorAll('.js-leave-open, #leaveOpenBtn, #leaveOpenBtnTop').forEach((btn) => {
    if (btn.dataset.leaveBound) return;
    btn.dataset.leaveBound = '1';
    btn.addEventListener('click', openLeaveModal);
  });
}

window.isHeatmapLeaveDay = isHeatmapLeaveDay;
window.initLeaveDays = initLeaveDays;
window.openLeaveModal = openLeaveModal;

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bindLeaveButton);
} else {
  bindLeaveButton();
}
