/* Fixed Heatmap order: Site → group label rows → members (dashboard-roster.json). */

/** @type {{ sites: Array<{ name: string, groups: Array<{ name: string, members: string[] }> }> }} */
let HEATMAP_ROSTER = { sites: [] };

async function initHeatmapRoster() {
  try {
    const resp = await fetch('dashboard-roster.json?' + Date.now());
    if (!resp.ok) return;
    const data = await resp.json();
    if (data && Array.isArray(data.sites)) HEATMAP_ROSTER = data;
  } catch (_) {
    /* keep empty → fallback sort */
  }
}

function sortEmployeesByMonthlyTotal(employees, matrix, days) {
  return [...employees].sort((a, b) => {
    const sumA = days.reduce((s, d) => s + (matrix[a]?.[d.key] || 0), 0);
    const sumB = days.reduce((s, d) => s + (matrix[b]?.[d.key] || 0), 0);
    return sumB - sumA || a.localeCompare(b);
  });
}

/**
 * @returns {Array<{ type: 'site'|'group'|'employee', label?: string, name?: string }>}
 */
function buildHeatmapRows(employees, matrix, days) {
  const pool = new Set(employees);
  Object.keys(matrix || {}).forEach((e) => pool.add(e));
  const all = [...pool];

  if (!HEATMAP_ROSTER.sites?.length) {
    return sortEmployeesByMonthlyTotal(all, matrix, days).map((name) => ({
      type: 'employee',
      name,
    }));
  }

  const rows = [];
  const used = new Set();

  HEATMAP_ROSTER.sites.forEach((site) => {
    const siteName = String(site.name || '').trim();
    if (!siteName) return;
    rows.push({ type: 'site', label: siteName });

    const addMember = (raw, groupName) => {
      const name = String(raw || '').trim();
      if (!name) return;
      used.add(name);
      rows.push({ type: 'employee', name, site: siteName, group: groupName || siteName });
    };

    (site.members || []).forEach((raw) => addMember(raw, siteName));

    (site.groups || []).forEach((group) => {
      const groupName = String(group.name || '').trim();
      if (!groupName) return;
      rows.push({ type: 'group', label: groupName, site: siteName });
      (group.members || []).forEach((raw) => addMember(raw, groupName));
    });
  });

  const extras = all.filter((e) => !used.has(e));
  if (extras.length) {
    rows.push({ type: 'group', label: '未在名册中' });
    sortEmployeesByMonthlyTotal(extras, matrix, days).forEach((name) => {
      rows.push({ type: 'employee', name });
    });
  }

  return rows;
}

window.initHeatmapRoster = initHeatmapRoster;
window.buildHeatmapRows = buildHeatmapRows;
