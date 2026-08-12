#!/usr/bin/env python3
"""
Daily Dashboard Automation
- Queries Databricks for monthly completed ticket counts
- Updates dashboard.html with fresh data
- On the 1st of each month, seals the previous month (summary + ticket details)
- Takes a screenshot of the dashboard
- Sends email with screenshot + table to luby.lu@nike.com
- Pushes to GitHub Pages
"""

import os, sys, json, re, subprocess, logging, shutil, argparse
from pathlib import Path
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from databricks_connect import ensure_databricks_http_path, run_with_hard_timeout
from dashboard_alerts import notify_failure, notify_push_failure

SCRIPT_DIR = Path(__file__).resolve().parent
CI_MODE = False
DISPLAY_TZ = ZoneInfo("Asia/Shanghai")


def now_display() -> datetime:
    """Dashboard timestamps are always shown in China Standard Time."""
    return datetime.now(DISPLAY_TZ)


def format_updated_time() -> str:
    return now_display().strftime("%Y/%-m/%-d %H:%M")


def today_display() -> date:
    return now_display().date()


def output_dir() -> Path:
    if CI_MODE:
        return Path(os.environ.get("GITHUB_WORKSPACE", SCRIPT_DIR.parent))
    return SCRIPT_DIR


load_dotenv(SCRIPT_DIR / ".env")

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(SCRIPT_DIR / ".browsers")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(SCRIPT_DIR / "dashboard.log"),
    ],
)
log = logging.getLogger(__name__)

TOKEN_EXPIRY = date(2026, 9, 8)
TOKEN_WARN_DAYS = 14

NAME_OVERRIDES = {
    "JQIANG": "Freddie Qiang",
    "AXu72": "Alex Xu",
    "YWei29": "Roy Wei",
    "AJian3": "Aaron Jiang",
    "HTan3": "Howie Tan",
    "HZh8": "Hooxi Zhu",
}

EMPLOYEE_IDS = [
    'BLiu60','AGuo22','AJian3','JDen4','HTan3','HFeng1',
    'LCh158','TTao5','L31','CLe144','RJu1','AXu72',
    'YWa456','HYip2','JCh603','KChu17','ALan2',
    'KOuYan','VCHE11','YWei29','LXIAN2',
    'JQIANG','HZhu8','DCha49','PWan61','YDin23','XZh302',
    'FL22','YY7','YZh33','Z36','GYo2',
    'HZh8',
]

DASHBOARD_SQL = (SCRIPT_DIR / "dashboard_query.sql").read_text()
WEEKLY_SQL = (SCRIPT_DIR / "weekly_query.sql").read_text()
TICKET_DETAILS_SQL = (SCRIPT_DIR / "ticket_details_query.sql").read_text()
_TS_YEAR = "YEAR(from_utc_timestamp(CURRENT_TIMESTAMP(), 'Asia/Shanghai'))"
_TS_MONTH = "MONTH(from_utc_timestamp(CURRENT_TIMESTAMP(), 'Asia/Shanghai'))"

QUERY_TIMEOUT_LOCAL = 900
QUERY_TIMEOUT_CI = 600


def query_timeout_seconds() -> int:
    if CI_MODE or os.environ.get("CI", "").lower() == "true":
        return QUERY_TIMEOUT_CI
    return QUERY_TIMEOUT_LOCAL


def build_ticket_details_sql(year: int, month: int) -> str:
    return (
        TICKET_DETAILS_SQL.replace(_TS_YEAR, str(year)).replace(_TS_MONTH, str(month))
    )


def build_dashboard_sql(year: int, month: int) -> str:
    return DASHBOARD_SQL.replace(_TS_YEAR, str(year)).replace(_TS_MONTH, str(month))


def _run_queries_once():
    from databricks import sql as dbsql

    skip_tickets = os.environ.get("SKIP_TICKETS") == "1"

    conn = dbsql.connect(
        server_hostname=os.environ["DATABRICKS_SERVER_HOSTNAME"],
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
    )
    cursor = conn.cursor()

    cursor.execute(DASHBOARD_SQL)
    monthly_rows = cursor.fetchall()
    monthly_cols = [d[0] for d in cursor.description]

    cursor.execute(WEEKLY_SQL)
    weekly_rows = cursor.fetchall()
    weekly_cols = [d[0] for d in cursor.description]

    ticket_rows = []
    ticket_cols = []
    if not skip_tickets:
        now = now_display()
        cursor.execute(build_ticket_details_sql(now.year, now.month))
        ticket_rows = cursor.fetchall()
        ticket_cols = [d[0] for d in cursor.description]
    else:
        log.info("SKIP_TICKETS=1: skipping ticket details query (using cache)")

    cursor.close()
    conn.close()

    monthly = [dict(zip(monthly_cols, r)) for r in monthly_rows]
    weekly = [dict(zip(weekly_cols, r)) for r in weekly_rows]
    tickets = [dict(zip(ticket_cols, r)) for r in ticket_rows]
    return monthly, weekly, tickets


def load_cached_ticket_details(month_key=None):
    if month_key is None:
        month_key = now_display().strftime("%Y-%m")
    tickets_path = output_dir() / "dashboard_tickets.json"
    if not tickets_path.exists():
        return []
    store = json.loads(tickets_path.read_text(encoding="utf-8"))
    return store.get(month_key, [])


def query_databricks():
    ensure_databricks_http_path(SCRIPT_DIR / ".env")

    for attempt in range(1, 3):
        log.info(f"Connecting to Databricks... (attempt {attempt}/2)")
        try:
            timeout = query_timeout_seconds()
            log.info(f"Query hard timeout: {timeout}s")
            monthly, weekly, tickets = run_with_hard_timeout(
                "databricks_query_worker:run_dashboard_queries",
                timeout,
                "Databricks dashboard queries",
            )
            log.info(f"Got {len(monthly)} monthly rows, {len(weekly)} weekly rows, {len(tickets)} ticket rows")
            return monthly, weekly, tickets
        except Exception as e:
            log.warning(f"Attempt {attempt} failed: {e}")
            if attempt < 2:
                log.info("Retrying in 10 seconds...")
                import time
                time.sleep(10)
            else:
                raise


def get_month_weeks():
    """Compute all Mon-Sun weeks overlapping the current month."""
    today = today_display()
    first_day = date(today.year, today.month, 1)
    if today.month == 12:
        last_day = date(today.year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(today.year, today.month + 1, 1) - timedelta(days=1)

    first_monday = first_day - timedelta(days=first_day.weekday())

    weeks = []
    current = first_monday
    while current <= last_day:
        weeks.append({
            "start": current,
            "end": current + timedelta(days=6),
        })
        current += timedelta(days=7)
    return weeks


def _to_date(val):
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str):
        return datetime.strptime(val[:10], "%Y-%m-%d").date()
    return val


def process_weekly_data(weekly_rows, monthly_rows):
    weeks = get_month_weeks()
    week_starts = [w["start"] for w in weeks]
    today = today_display()

    weekly_lookup = {}
    for row in weekly_rows:
        eid = row["employee_id"]
        ws = _to_date(row["week_start"])
        if eid not in weekly_lookup:
            weekly_lookup[eid] = {}
        weekly_lookup[eid][ws] = {
            "inc": int(row["incident_count"] or 0),
            "task": int(row["task_count"] or 0),
        }

    result_data = []
    for emp in monthly_rows:
        eid = emp["employee_id"]
        name = emp["employee_name"]
        weekly_cells = [
            weekly_lookup.get(eid, {}).get(ws, {"inc": 0, "task": 0})
            for ws in week_starts
        ]
        tot_inc = sum(c["inc"] for c in weekly_cells)
        tot_task = sum(c["task"] for c in weekly_cells)
        result_data.append({
            "employee": name,
            "weekly": weekly_cells,
            "total": {"inc": tot_inc, "task": tot_task},
        })

    week_labels = []
    for w in weeks:
        s, e = w["start"], w["end"]
        if s.month == e.month:
            week_labels.append(f"{s.month}/{s.day}-{e.day}")
        else:
            week_labels.append(f"{s.month}/{s.day}-{e.month}/{e.day}")

    current_week = None
    for i, w in enumerate(weeks):
        if w["start"] <= today <= w["end"]:
            current_week = i
            break

    return {
        "weeks": week_labels,
        "currentWeek": current_week,
        "data": result_data,
    }


def apply_name_overrides(rows):
    for row in rows:
        eid = row["employee_id"]
        if eid in NAME_OVERRIDES:
            row["employee_name"] = NAME_OVERRIDES[eid]
        if row["employee_name"] == eid:
            row["employee_name"] = NAME_OVERRIDES.get(eid, eid)
    return rows


def process_ticket_details(ticket_rows, monthly_rows):
    name_by_id = {r["employee_id"]: r["employee_name"] for r in monthly_rows}
    tickets = []
    for row in ticket_rows:
        eid = row["employee_id"]
        name = NAME_OVERRIDES.get(eid, name_by_id.get(eid, row.get("employee_name", eid)))
        closed = _to_date(row["closed_date"])
        tickets.append({
            "employee": name,
            "type": "Incident" if row["ticket_type"] == "incident" else "SC Task",
            "number": row["ticket_number"],
            "closed": closed.isoformat() if closed else "",
        })
    tickets.sort(key=lambda t: (t["employee"], t["type"], t["number"]))
    return tickets


def monthly_summary_from_tickets(tickets):
    """Build monthly employee totals from ticket detail rows."""
    totals = {}
    for t in tickets:
        emp = t["employee"]
        if emp not in totals:
            totals[emp] = {"employee": emp, "incidents": 0, "tasks": 0}
        if t["type"] == "Incident":
            totals[emp]["incidents"] += 1
        else:
            totals[emp]["tasks"] += 1
    return sorted(
        totals.values(),
        key=lambda r: (-(r["incidents"] + r["tasks"]), r["employee"]),
    )


def notify_failure_safe(job_name: str, error: Exception) -> None:
    if CI_MODE:
        log.error("%s failed: %s", job_name, error)
        return
    notify_failure(job_name, error)


def refresh_history_monthly(month_key, rows):
    """Refresh monthly summary in dashboard_history.json for one month."""
    history_path = output_dir() / "dashboard_history.json"
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
        except Exception:
            history = {}
    else:
        history = {}

    monthly = [
        {
            "employee": r["employee_name"],
            "incidents": int(r["incident_count"]),
            "tasks": int(r["task_count"]),
        }
        for r in rows
    ]
    prev = history.get(month_key, {})
    history[month_key] = {
        **prev,
        "monthly": monthly,
    }
    history_path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    log.info(f"dashboard_history.json monthly refreshed for {month_key}")
    return history_path


def update_dashboard_tickets(tickets, month_key=None):
    if month_key is None:
        month_key = now_display().strftime("%Y-%m")
    tickets_path = output_dir() / "dashboard_tickets.json"

    if tickets_path.exists():
        try:
            store = json.loads(tickets_path.read_text(encoding="utf-8"))
        except Exception:
            store = {}
    else:
        repo_tickets = Path(GITHUB_REPO_DIR) / "dashboard_tickets.json"
        if repo_tickets.exists():
            store = json.loads(repo_tickets.read_text(encoding="utf-8"))
        else:
            store = {}

    store[month_key] = tickets
    tickets_path.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    log.info(f"dashboard_tickets.json updated for {month_key} ({len(tickets)} tickets)")
    return tickets_path


MONTH_SEAL_STAMP = SCRIPT_DIR / ".month_seal_stamp"


def fetch_dashboard_for_month(year: int, month: int):
    from databricks import sql as dbsql

    ensure_databricks_http_path(SCRIPT_DIR / ".env")
    conn = dbsql.connect(
        server_hostname=os.environ["DATABRICKS_SERVER_HOSTNAME"],
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
    )
    cursor = conn.cursor()
    cursor.execute(build_dashboard_sql(year, month))
    rows = cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    cursor.close()
    conn.close()
    return [dict(zip(cols, r)) for r in rows]


def fetch_tickets_for_month(year: int, month: int):
    from databricks import sql as dbsql

    ensure_databricks_http_path(SCRIPT_DIR / ".env")
    conn = dbsql.connect(
        server_hostname=os.environ["DATABRICKS_SERVER_HOSTNAME"],
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
    )
    cursor = conn.cursor()
    cursor.execute(build_ticket_details_sql(year, month))
    rows = cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    cursor.close()
    conn.close()
    return [dict(zip(cols, r)) for r in rows]


def backfill_month(month_key: str) -> int:
    """Refresh monthly summary and ticket details for one month."""
    year, month = map(int, month_key.split("-"))
    log.info(f"Backfilling {month_key}: monthly summary...")
    monthly_rows = apply_name_overrides(fetch_dashboard_for_month(year, month))
    refresh_history_monthly(month_key, monthly_rows)

    log.info(f"Backfilling {month_key}: ticket details...")
    raw = fetch_tickets_for_month(year, month)
    tickets = process_ticket_details(raw, monthly_rows)
    update_dashboard_tickets(tickets, month_key=month_key)
    log.info(f"Backfilled {month_key}: {len(tickets)} tickets")
    return len(tickets)


def seal_previous_month_if_needed():
    """On the 1st, re-query and lock the previous month's data once per day."""
    today = today_display()
    if today.day != 1:
        return

    prev = date(today.year, today.month, 1) - timedelta(days=1)
    month_key = prev.strftime("%Y-%m")

    if MONTH_SEAL_STAMP.exists():
        try:
            if MONTH_SEAL_STAMP.read_text(encoding="utf-8").strip() == month_key:
                log.info(f"Previous month {month_key} already sealed today")
                return
        except Exception:
            pass

    log.info(f"=== Sealing previous month: {month_key} ===")
    try:
        backfill_month(month_key)
        MONTH_SEAL_STAMP.write_text(month_key, encoding="utf-8")
        log.info(f"=== Month seal complete: {month_key} ===")
    except Exception as e:
        log.error(f"Month seal failed for {month_key}: {e}")
        notify_failure_safe("SN Dashboard Month Seal", e)


def update_html(rows, weekly_info=None):
    template_path = SCRIPT_DIR / "dashboard.html"
    html_path = (output_dir() / "index.html") if CI_MODE else (SCRIPT_DIR / "dashboard.html")
    html = template_path.read_text(encoding="utf-8")

    js_entries = []
    for r in rows:
        name = r["employee_name"].replace("'", "\\'")
        js_entries.append(
            f"  {{ employee: '{name}', incidents: {r['incident_count']}, tasks: {r['task_count']} }}"
        )
    new_data = "let DATA = [\n" + ",\n".join(js_entries) + "\n];"

    html = re.sub(
        r"let DATA = \[.*?\];",
        new_data,
        html,
        flags=re.DOTALL,
    )

    if weekly_info:
        weekly_js = json.dumps(weekly_info, ensure_ascii=False)
        html = re.sub(
            r"let WEEKLY_DATA = \{.*?\};",
            f"let WEEKLY_DATA = {weekly_js};",
            html,
            flags=re.DOTALL,
        )

    now_str = format_updated_time()
    html = re.sub(
        r'(<div class="updated" id="updatedTime">).*?(</div>)',
        rf'\1Updated: {now_str}\2',
        html,
    )

    month_key = now_display().strftime("%Y-%m")
    html = re.sub(
        r'let CURRENT_MONTH_KEY = ".*?";',
        f'let CURRENT_MONTH_KEY = "{month_key}";',
        html,
    )

    html_path.write_text(html, encoding="utf-8")
    log.info("%s updated", html_path.name)
    return html_path


def update_dashboard_history(rows, weekly_info=None):
    """Persist current month snapshot for month-picker on GitHub Pages."""
    month_key = now_display().strftime("%Y-%m")
    month_label = now_display().strftime("%B %Y")
    history_path = output_dir() / "dashboard_history.json"

    if history_path.exists():
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
        except Exception:
            history = {}
    else:
        repo_history = Path(GITHUB_REPO_DIR) / "dashboard_history.json"
        if repo_history.exists():
            history = json.loads(repo_history.read_text(encoding="utf-8"))
        else:
            history = {}

    monthly = [
        {
            "employee": r["employee_name"],
            "incidents": int(r["incident_count"]),
            "tasks": int(r["task_count"]),
        }
        for r in rows
    ]

    prev = history.get(month_key, {})
    history[month_key] = {
        "label": month_label,
        "monthly": monthly,
        "weekly": weekly_info or prev.get("weekly", {"weeks": [], "data": []}),
    }

    history_path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    log.info(f"dashboard_history.json updated for {month_key}")
    return history_path


def take_screenshot(html_path):
    from playwright.sync_api import sync_playwright

    png_path = SCRIPT_DIR / "dashboard_screenshot.png"
    log.info("Taking screenshot with Playwright...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"file://{html_path}")
        page.wait_for_timeout(2000)

        full_height = page.evaluate("document.body.scrollHeight")
        page.set_viewport_size({"width": 1280, "height": full_height + 50})
        page.wait_for_timeout(500)

        page.screenshot(path=str(png_path), full_page=True)
        browser.close()

    log.info(f"Screenshot saved: {png_path}")
    return png_path


def build_html_table(rows):
    now_str = now_display().strftime("%Y-%m-%d %H:%M")
    total_inc = sum(r["incident_count"] for r in rows)
    total_task = sum(r["task_count"] for r in rows)
    month_label = now_display().strftime("%B %Y")

    html = f"""
    <div style="font-family:system-ui,-apple-system,sans-serif;max-width:800px">
      <h2 style="color:#1e3a5f">Monthly Completed Tickets - {month_label}</h2>
      <p style="color:#64748b;font-size:13px">Data updated: {now_str} CST</p>
      <table style="border-collapse:collapse;width:100%;font-size:13px">
        <tr style="background:#1e3a5f;color:#fff">
          <th style="padding:8px 12px;text-align:left">Employee</th>
          <th style="padding:8px 12px;text-align:right">Incidents</th>
          <th style="padding:8px 12px;text-align:right">SC Tasks</th>
          <th style="padding:8px 12px;text-align:right;font-weight:bold">Total</th>
        </tr>"""

    for i, r in enumerate(rows):
        total = r["incident_count"] + r["task_count"]
        bg = "#f8fafc" if i % 2 == 0 else "#fff"
        html += f"""
        <tr style="background:{bg}">
          <td style="padding:6px 12px">{r['employee_name']}</td>
          <td style="padding:6px 12px;text-align:right;color:#3b82f6">{r['incident_count']}</td>
          <td style="padding:6px 12px;text-align:right;color:#f97316">{r['task_count']}</td>
          <td style="padding:6px 12px;text-align:right;font-weight:bold">{total}</td>
        </tr>"""

    html += f"""
        <tr style="background:#1e3a5f;color:#fff;font-weight:bold">
          <td style="padding:8px 12px">TOTAL</td>
          <td style="padding:8px 12px;text-align:right">{total_inc}</td>
          <td style="padding:8px 12px;text-align:right">{total_task}</td>
          <td style="padding:8px 12px;text-align:right">{total_inc + total_task}</td>
        </tr>
      </table>
    </div>"""
    return html


def send_email_applescript(subject, html_body, screenshot_path, recipient):
    log.info(f"Sending email via Outlook to {recipient}...")
    screenshot_posix = str(screenshot_path)

    ascript = f'''
    tell application "Microsoft Outlook"
        set newMsg to make new outgoing message with properties {{subject:"{subject}", content:"{html_body.replace('"', '\\"').replace(chr(10), "")}"}}
        make new to recipient at newMsg with properties {{email address:{{address:"{recipient}"}}}}
        make new attachment at newMsg with properties {{file:POSIX file "{screenshot_posix}"}}
        send newMsg
    end tell
    '''
    result = subprocess.run(["osascript", "-e", ascript], capture_output=True, text=True)
    if result.returncode != 0:
        log.warning(f"Outlook AppleScript failed: {result.stderr}")
        return send_email_open_mailto(subject, recipient)
    log.info("Email sent via Outlook")
    return True


GITHUB_REPO_DIR = str(Path.home() / ".sn-dashboard-repo")
GITHUB_REPO_URL = "https://github.com/TcxddyLuLu/sn-dashboard.git"
GITHUB_USER = "TcxddyLuLu"


def _ensure_gh_user():
    """Ensure the dedicated .gh config always uses the personal account."""
    hosts_path = SCRIPT_DIR / ".gh" / "hosts.yml"
    if not hosts_path.exists():
        return
    try:
        content = hosts_path.read_text()
        expected = f"    user: {GITHUB_USER}"
        if expected in content:
            return
        new_content = re.sub(r"(    user: )\S+", rf"\g<1>{GITHUB_USER}", content)
        if new_content != content:
            log.info(f"Fixing gh active user to {GITHUB_USER}")
            hosts_path.write_text(new_content)
    except Exception as e:
        log.warning(f"Could not fix gh user: {e}")


def _ensure_git_config(repo_dir):
    """Ensure git user config exists in the repo (needed for commit)."""
    rd = str(repo_dir)
    result = subprocess.run(
        ["git", "-C", rd, "config", "user.name"], capture_output=True, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        subprocess.run(["git", "-C", rd, "config", "user.name", GITHUB_USER], capture_output=True)
        subprocess.run(
            ["git", "-C", rd, "config", "user.email", "tcxddylulu@users.noreply.github.com"],
            capture_output=True,
        )
        log.info("Git user config set in repo")

    result = subprocess.run(
        ["git", "-C", rd, "config", "credential.helper"], capture_output=True, text=True
    )
    if "gh auth git-credential" not in result.stdout:
        gh_config = str(SCRIPT_DIR / ".gh")
        subprocess.run(
            ["git", "-C", rd, "config", "credential.helper", ""],
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", rd, "config", "--add", "credential.helper",
             f"!GH_CONFIG_DIR='{gh_config}' gh auth git-credential"],
            capture_output=True,
        )
        log.info("Git credential helper set to use gh with personal account")


def push_to_github(html_path) -> bool:
    _ensure_gh_user()

    gh_config = str(SCRIPT_DIR / ".gh")
    env = {**os.environ, "GH_CONFIG_DIR": gh_config}

    repo_dir = Path(GITHUB_REPO_DIR)
    if not (repo_dir / ".git").exists():
        log.info("Cloning GitHub repo...")
        result = subprocess.run(
            ["git", "clone", GITHUB_REPO_URL, str(repo_dir)],
            env=env, capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.warning(f"Git clone failed: {result.stderr}")
            notify_push_failure("SN Dashboard", "git clone", result.stderr)
            return False

    _ensure_git_config(repo_dir)

    pull_result = subprocess.run(
        ["git", "-C", str(repo_dir), "pull", "--rebase", "--autostash"],
        env=env, capture_output=True, text=True,
    )
    if pull_result.returncode != 0:
        log.warning(f"Git pull failed: {pull_result.stderr}")
        notify_push_failure("SN Dashboard", "git pull", pull_result.stderr)
        return False

    shutil.copy2(str(html_path), str(repo_dir / "index.html"))

    static_files = [
        "chart.min.js",
        "dashboard-features.js",
        "dashboard_history.json",
        "dashboard_tickets.json",
        "xlsx.full.min.js",
    ]
    for fname in static_files:
        src = SCRIPT_DIR / fname
        if src.exists():
            shutil.copy2(str(src), str(repo_dir / fname))

    subprocess.run(
        ["git", "-C", str(repo_dir), "add", "index.html", *static_files],
        capture_output=True,
    )

    diff_result = subprocess.run(
        ["git", "-C", str(repo_dir), "diff", "--cached", "--quiet"],
        capture_output=True,
    )
    if diff_result.returncode == 0:
        log.info("GitHub Pages: no changes to push")
        return True

    now_str = now_display().strftime("%Y-%m-%d %H:%M")
    commit_result = subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-m", f"Update dashboard {now_str} CST"],
        capture_output=True, text=True, env=env,
    )
    if commit_result.returncode != 0:
        log.warning(f"Git commit failed: {commit_result.stderr}")
        notify_push_failure("SN Dashboard", "git commit", commit_result.stderr)
        return False

    push_result = subprocess.run(
        ["git", "-C", str(repo_dir), "push"],
        capture_output=True, text=True, env=env,
    )
    if push_result.returncode == 0:
        log.info("GitHub Pages updated successfully")
        return True

    if "rejected" in (push_result.stderr or "").lower():
        log.warning("Push rejected, retrying after pull --rebase --autostash")
        retry_pull = subprocess.run(
            ["git", "-C", str(repo_dir), "pull", "--rebase", "--autostash"],
            env=env, capture_output=True, text=True,
        )
        if retry_pull.returncode == 0:
            push_result = subprocess.run(
                ["git", "-C", str(repo_dir), "push"],
                capture_output=True, text=True, env=env,
            )
            if push_result.returncode == 0:
                log.info("GitHub Pages updated successfully (after retry)")
                return True

    log.warning(f"GitHub push failed: {push_result.stderr}")
    notify_push_failure("SN Dashboard", "git push", push_result.stderr)
    return False


def push_to_github_ci() -> bool:
    repo_dir = output_dir()
    subprocess.run(
        ["git", "config", "user.name", "github-actions[bot]"],
        cwd=repo_dir, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
        cwd=repo_dir, check=True,
    )

    files = ["index.html", "dashboard_history.json", "dashboard_tickets.json"]
    subprocess.run(["git", "add", *files], cwd=repo_dir, check=True)

    if subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=repo_dir
    ).returncode == 0:
        log.info("GitHub Pages: no changes to push")
        return True

    now_str = now_display().strftime("%Y-%m-%d %H:%M")
    subprocess.run(
        ["git", "commit", "-m", f"Update dashboard {now_str} CST"],
        cwd=repo_dir, check=True,
    )
    subprocess.run(["git", "push"], cwd=repo_dir, check=True)
    log.info("GitHub Pages updated successfully")
    return True


def send_email_open_mailto(subject, recipient):
    log.warning("Falling back to mailto: link (manual send required)")
    import urllib.parse
    url = f"mailto:{recipient}?subject={urllib.parse.quote(subject)}&body={urllib.parse.quote('Please see attached dashboard screenshot.')}"
    subprocess.run(["open", url])
    return False


def main():
    global CI_MODE
    parser = argparse.ArgumentParser(description="SN Dashboard automation")
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI mode: skip email/screenshot, commit to GITHUB_WORKSPACE",
    )
    args = parser.parse_args()
    CI_MODE = args.ci or os.environ.get("CI", "").lower() == "true"

    now = now_display()
    month_label = now.strftime("%B %Y")
    now_str = now.strftime("%Y-%m-%d %H:%M")

    log.info(f"=== Dashboard automation started at {now_str} (ci={CI_MODE}) ===")

    seal_previous_month_if_needed()

    try:
        rows, weekly_rows, ticket_rows = query_databricks()
    except Exception as e:
        log.error(f"Databricks query failed: {e}")
        notify_failure_safe("SN Dashboard", e)
        sys.exit(1)

    rows = apply_name_overrides(rows)

    rows.sort(key=lambda r: (-(r["incident_count"] + r["task_count"]), r["employee_id"]))

    weekly_info = process_weekly_data(weekly_rows, rows)
    if os.environ.get("SKIP_TICKETS") == "1":
        ticket_details = load_cached_ticket_details()
        log.info(f"Using cached ticket details ({len(ticket_details)} tickets)")
    else:
        ticket_details = process_ticket_details(ticket_rows, rows)
        update_dashboard_tickets(ticket_details)

    update_dashboard_history(rows, weekly_info)
    html_path = update_html(rows, weekly_info)

    if not CI_MODE:
        try:
            screenshot_path = take_screenshot(html_path)
        except Exception as e:
            log.error(f"Screenshot failed: {e}")
            screenshot_path = None

        table_html = build_html_table(rows)
        subject = f"[Dashboard] Monthly Completed Tickets - {month_label} (Updated {now_str})"
        recipient = os.environ.get("EMAIL_TO", "luby.lu@nike.com")

        days_left = (TOKEN_EXPIRY - today_display()).days
        if days_left <= TOKEN_WARN_DAYS:
            warning = (
                f'<div style="background:#fef2f2;border:2px solid #ef4444;border-radius:8px;'
                f'padding:12px 16px;margin-bottom:16px;font-size:14px;color:#991b1b">'
                f'<strong>⚠ Databricks Token 即将到期！</strong><br>'
                f'Token 将在 <strong>{days_left} 天后（{TOKEN_EXPIRY}）</strong>过期。'
                f'请尽快在 Cursor 中让我帮你续期，否则自动化将停止工作。</div>'
            )
            table_html = warning + table_html
            subject = f"[⚠ TOKEN {days_left}d] " + subject
            log.warning(f"Token expires in {days_left} days!")

        if screenshot_path:
            send_email_applescript(subject, table_html, screenshot_path, recipient)
        else:
            log.warning("No screenshot available, skipping email")

        if not push_to_github(html_path):
            log.error("GitHub Pages push failed; public dashboard may be stale")
    else:
        if not push_to_github_ci():
            log.error("GitHub Pages push failed in CI")
            sys.exit(1)

    log.info("=== Dashboard automation completed ===")


if __name__ == "__main__":
    main()
