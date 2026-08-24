#!/usr/bin/env python3
"""
Daily Dashboard Automation
- Queries Databricks for monthly completed ticket counts
- Updates dashboard.html with fresh data
- On the 1st of each month, seals the previous month (summary + ticket details)
- Pushes to GitHub Pages
"""

import os, sys, json, re, subprocess, logging, shutil, argparse, fcntl, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from databricks_connect import ensure_databricks_http_path, run_with_hard_timeout
from dashboard_alerts import notify_failure, notify_push_failure, notify_ticket_failure_streak

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

_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
if os.environ.get("CI", "").lower() != "true":
    _log_handlers.append(logging.FileHandler(SCRIPT_DIR / "dashboard.log"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_log_handlers,
)
log = logging.getLogger(__name__)

TOKEN_EXPIRY = date(2026, 9, 8)
TOKEN_WARN_DAYS = 14
TICKET_FAIL_ALERT_THRESHOLD = 3
TICKET_HEALTH_FILE = SCRIPT_DIR / "ticket_query_health.json"
TICKET_META_EXCEL_UPDATED = "_excel_updated_at"
TICKET_MORNING_START_HOUR = 9
TICKET_AFTERNOON_HOUR = 17
DASHBOARD_LAST_HOUR = 17
EXCEL_CATCHUP_DELAYS_MIN = (15, 30)
EXCEL_LOCK_FILE = SCRIPT_DIR / ".excel_refresh.lock"
DASHBOARD_LOCK_FILE = SCRIPT_DIR / ".dashboard_refresh.lock"
DATABRICKS_QUERY_LOCK_FILE = SCRIPT_DIR / ".databricks_query.lock"
QUERY_TIMEOUT_SUMMARY_LOCAL = 600
GIT_NETWORK_TIMEOUT_SEC = 120
SQL_PLACEHOLDER_RE = re.compile(r"__MONTH_[A-Z_]+__|\{_MONTH_[A-Z_]+\}")

NAME_OVERRIDES = {
    "JQIANG": "Freddie Qiang",
    "AXu72": "Alex Xu",
    "YWei29": "Roy Wei",
    "AJian3": "Aaron Jiang",
    "HTan3": "Howie Tan",
    "HZh8": "Hooxi Zhu",
}

# ServiceNow assigned_to values for employees missing/wrong in sys_user (matches dashboard_query.sql).
MANUAL_ASSIGNED_TO_NAMES = {
    "HTan3": "Howie Tan",
    "AJian3": "Aaron Jiang",
}

EMPLOYEE_IDS = [
    'BLiu60','AGuo22','AJian3','JDen4','HTan3','HFeng1',
    'LCh158','TTao5','L31','CLe144','RJu1','AXu72',
    'YWa456','HYip2','JCh603','KChu17','ALan2',
    'KOuYan','VCHE11','YWei29','LXIAN2',
    'JQIANG','HZhu8','DCha49','PWan61','YDin23','XZh302',
    'FL22','YY7','YZh33','Z36','GYo2',
    'HZh8','PZh105',
]

DASHBOARD_SQL = (SCRIPT_DIR / "dashboard_query.sql").read_text()
WEEKLY_SQL = (SCRIPT_DIR / "weekly_query.sql").read_text()
DAILY_SQL = (SCRIPT_DIR / "daily_query.sql").read_text()
TICKET_DETAILS_SQL = (SCRIPT_DIR / "ticket_details_query.sql").read_text()
TEAM_INCIDENTS_SQL = (SCRIPT_DIR / "team_incidents_query.sql").read_text()
TEAM_TASKS_SQL = (SCRIPT_DIR / "team_tasks_query.sql").read_text()
TEAM_MEMBER_INCIDENT_SQL = (SCRIPT_DIR / "team_member_incident.sql").read_text()
TEAM_MEMBER_TASK_SQL = (SCRIPT_DIR / "team_member_task.sql").read_text()
_EMPLOYEE_ID_PH = "__EMPLOYEE_ID__"
_EMPLOYEE_NAME_PH = "__EMPLOYEE_NAME__"
_TS_YEAR = "YEAR(from_utc_timestamp(CURRENT_TIMESTAMP(), 'Asia/Shanghai'))"
_TS_MONTH = "MONTH(from_utc_timestamp(CURRENT_TIMESTAMP(), 'Asia/Shanghai'))"
_MONTH_START_PH = "__MONTH_START__"
_MONTH_END_PH = "__MONTH_END__"
_MONTH_START_DATE_PH = "__MONTH_START_DATE__"
_MONTH_END_DATE_PH = "__MONTH_END_DATE__"
_UPPER_EMPLOYEE_IDS_PH = "__UPPER_EMPLOYEE_IDS__"
_EMPLOYEE_ID_LIST_PH = "__EMPLOYEE_ID_LIST__"
_MONTH_START_UTC_PH = "__MONTH_START_UTC__"
_MONTH_END_UTC_PH = "__MONTH_END_UTC__"

QUERY_TIMEOUT_SUMMARY_CI = 300
QUERY_TIMEOUT_TICKETS_LOCAL = 1200
QUERY_TIMEOUT_TICKETS_CI = 1500
QUERY_TIMEOUT_TEAM_LOCAL = 900
QUERY_TIMEOUT_TEAM_CI = 900

CANONICAL_EMPLOYEE_ID = {employee_id.upper(): employee_id for employee_id in EMPLOYEE_IDS}


def summary_timeout_seconds() -> int:
    if CI_MODE or os.environ.get("CI", "").lower() == "true":
        return QUERY_TIMEOUT_SUMMARY_CI
    return QUERY_TIMEOUT_SUMMARY_LOCAL


def ticket_timeout_seconds() -> int:
    if CI_MODE or os.environ.get("CI", "").lower() == "true":
        return QUERY_TIMEOUT_TICKETS_CI
    return QUERY_TIMEOUT_TICKETS_LOCAL


def team_query_timeout_seconds() -> int:
    if CI_MODE or os.environ.get("CI", "").lower() == "true":
        return QUERY_TIMEOUT_TEAM_CI
    return QUERY_TIMEOUT_TEAM_LOCAL


def canonical_employee_id(raw_id: str) -> str:
    return CANONICAL_EMPLOYEE_ID.get((raw_id or "").upper(), raw_id)


def month_date_range(year: int, month: int) -> tuple[str, str]:
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1:04d}-01-01"
    else:
        end = f"{year:04d}-{month + 1:02d}-01"
    return start, end


def month_utc_bounds(year: int, month: int) -> tuple[str, str]:
    """Inclusive month start and exclusive month end as UTC timestamp literals."""
    start_cst = datetime(year, month, 1, tzinfo=DISPLAY_TZ)
    if month == 12:
        end_cst = datetime(year + 1, 1, 1, tzinfo=DISPLAY_TZ)
    else:
        end_cst = datetime(year, month + 1, 1, tzinfo=DISPLAY_TZ)
    start_utc = start_cst.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = end_cst.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return start_utc, end_utc


def build_team_ticket_sql(template: str, year: int, month: int) -> str:
    employee_ids = ", ".join(f"'{employee_id}'" for employee_id in EMPLOYEE_IDS)
    start_utc, end_utc = month_utc_bounds(year, month)
    return (
        template.replace(_EMPLOYEE_ID_LIST_PH, employee_ids)
        .replace(_MONTH_START_UTC_PH, start_utc)
        .replace(_MONTH_END_UTC_PH, end_utc)
    )


def build_team_tickets_sql(year: int, month: int) -> tuple[str, str]:
    """Return incident and task SQL for parallel team ticket fetch."""
    return (
        build_team_ticket_sql(TEAM_INCIDENTS_SQL, year, month),
        build_team_ticket_sql(TEAM_TASKS_SQL, year, month),
    )


def sql_literal(value: str) -> str:
    return (value or "").replace("'", "''")


def build_member_ticket_sql(template: str, employee_id: str, employee_name: str, year: int, month: int) -> str:
    return (
        template.replace(_EMPLOYEE_ID_PH, sql_literal(employee_id))
        .replace(_EMPLOYEE_NAME_PH, sql_literal(employee_name))
        .replace("__TS_YEAR__", str(year))
        .replace("__TS_MONTH__", str(month))
    )


def _load_employee_names_for_tickets() -> list[tuple[str, str]]:
    ids = ", ".join(f"'{employee_id}'" for employee_id in EMPLOYEE_IDS)
    lookup_sql = (
        "SELECT u.user_name AS employee_id, u.name AS employee_name "
        "FROM published_domain.rese_prd_servicenow.sys_user u "
        f"WHERE u.user_name IN ({ids})"
    )
    try:
        rows = _execute_team_ticket_sql(lookup_sql)
        by_id = {row["employee_id"]: row["employee_name"] for row in rows}
    except Exception as exc:
        log.warning("sys_user name lookup failed, using overrides only: %s", exc)
        by_id = {}

    names: list[tuple[str, str]] = []
    for employee_id in EMPLOYEE_IDS:
        if employee_id in MANUAL_ASSIGNED_TO_NAMES:
            employee_name = MANUAL_ASSIGNED_TO_NAMES[employee_id]
        else:
            # Prefer sys_user.name — it matches incident/task assigned_to in ServiceNow.
            # NAME_OVERRIDES are display-only and must not be used for SQL lookup.
            employee_name = by_id.get(employee_id) or employee_id
        names.append((employee_id, employee_name))
    return names


def _run_team_tickets_by_member(year: int, month: int) -> list[dict]:
    """Fetch tickets with one small query per employee (avoids full-table scan)."""
    members = _load_employee_names_for_tickets()
    jobs: list[str] = []
    for employee_id, employee_name in members:
        jobs.append(
            build_member_ticket_sql(
                TEAM_MEMBER_INCIDENT_SQL, employee_id, employee_name, year, month
            )
        )
        jobs.append(
            build_member_ticket_sql(
                TEAM_MEMBER_TASK_SQL, employee_id, employee_name, year, month
            )
        )

    ticket_rows: list[dict] = []
    workers = min(8, max(1, len(jobs)))
    log.info(
        "Running %s per-employee ticket queries (%s members, %s workers)...",
        len(jobs),
        len(members),
        workers,
    )
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for chunk in pool.map(_execute_team_ticket_sql, jobs):
            ticket_rows.extend(chunk)
    log.info("Per-employee ticket queries returned %s rows", len(ticket_rows))
    return ticket_rows


def normalize_ticket_rows(rows: list[dict]) -> list[dict]:
    """Canonicalize employee IDs and drop duplicate ticket rows."""
    seen: set[tuple[str, str, str]] = set()
    normalized: list[dict] = []
    for row in rows:
        employee_id = canonical_employee_id(row.get("employee_id", ""))
        if employee_id not in EMPLOYEE_IDS:
            continue
        ticket_number = row.get("ticket_number")
        ticket_type = row.get("ticket_type")
        dedupe_key = (employee_id, ticket_number, ticket_type)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append({
            **row,
            "employee_id": employee_id,
            "employee_name": NAME_OVERRIDES.get(
                employee_id, row.get("employee_name") or employee_id
            ),
        })
    return normalized


def build_monthly_rows(ticket_rows: list[dict]) -> list[dict]:
    normalized = normalize_ticket_rows(ticket_rows)
    counts = {employee_id: {"incident_count": 0, "task_count": 0} for employee_id in EMPLOYEE_IDS}
    names = {employee_id: NAME_OVERRIDES.get(employee_id, employee_id) for employee_id in EMPLOYEE_IDS}
    for row in normalized:
        employee_id = row["employee_id"]
        names[employee_id] = NAME_OVERRIDES.get(
            employee_id, row.get("employee_name") or names[employee_id]
        )
        if row["ticket_type"] == "incident":
            counts[employee_id]["incident_count"] += 1
        else:
            counts[employee_id]["task_count"] += 1

    monthly_rows = []
    for employee_id in EMPLOYEE_IDS:
        incident_count = counts[employee_id]["incident_count"]
        task_count = counts[employee_id]["task_count"]
        monthly_rows.append({
            "employee_id": employee_id,
            "employee_name": names[employee_id],
            "incident_count": incident_count,
            "task_count": task_count,
            "total_count": incident_count + task_count,
        })
    return monthly_rows


def aggregate_weekly_rows(ticket_rows: list[dict]) -> list[dict]:
    normalized = normalize_ticket_rows(ticket_rows)
    tally: dict[tuple[str, date], dict[str, int]] = {}
    for row in normalized:
        closed = _to_date(row["closed_date"])
        if not closed:
            continue
        week_start = closed - timedelta(days=closed.weekday())
        key = (row["employee_id"], week_start)
        if key not in tally:
            tally[key] = {"incident_count": 0, "task_count": 0}
        if row["ticket_type"] == "incident":
            tally[key]["incident_count"] += 1
        else:
            tally[key]["task_count"] += 1
    return [
        {"employee_id": employee_id, "week_start": week_start, **counts}
        for (employee_id, week_start), counts in sorted(tally.items())
    ]


def aggregate_daily_rows(ticket_rows: list[dict]) -> list[dict]:
    normalized = normalize_ticket_rows(ticket_rows)
    tally: dict[tuple[str, date], dict[str, int]] = {}
    for row in normalized:
        closed = _to_date(row["closed_date"])
        if not closed:
            continue
        key = (row["employee_id"], closed)
        if key not in tally:
            tally[key] = {"incident_count": 0, "task_count": 0}
        if row["ticket_type"] == "incident":
            tally[key]["incident_count"] += 1
        else:
            tally[key]["task_count"] += 1
    return [
        {
            "employee_id": employee_id,
            "closed_date": closed,
            "incident_count": counts["incident_count"],
            "task_count": counts["task_count"],
            "total_count": counts["incident_count"] + counts["task_count"],
        }
        for (employee_id, closed), counts in sorted(tally.items())
    ]


def build_ticket_details_sql(year: int, month: int) -> str:
    month_start = f"to_timestamp('{year:04d}-{month:02d}-01')"
    month_end = f"add_months(to_timestamp('{year:04d}-{month:02d}-01'), 1)"
    return (
        TICKET_DETAILS_SQL.replace(_MONTH_START_PH, month_start).replace(_MONTH_END_PH, month_end)
    )


def validate_ticket_sql(year: int, month: int) -> str:
    """Ensure ticket SQL placeholders were substituted before hitting Databricks."""
    sql = build_ticket_details_sql(year, month)
    if _MONTH_START_PH in sql or _MONTH_END_PH in sql or SQL_PLACEHOLDER_RE.search(sql):
        raise RuntimeError("Ticket SQL still contains unreplaced month placeholders")
    return sql


def load_ticket_health() -> dict:
    if not TICKET_HEALTH_FILE.exists():
        return {"consecutive_failures": 0}
    try:
        return json.loads(TICKET_HEALTH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"consecutive_failures": 0}


def save_ticket_health(health: dict) -> None:
    TICKET_HEALTH_FILE.write_text(json.dumps(health, ensure_ascii=False, indent=2), encoding="utf-8")


def record_ticket_query_success() -> None:
    health = load_ticket_health()
    health["consecutive_failures"] = 0
    health["last_success"] = now_display().strftime("%Y-%m-%d %H:%M")
    health.pop("last_error", None)
    save_ticket_health(health)


def record_ticket_query_failure(error: str) -> int:
    health = load_ticket_health()
    streak = int(health.get("consecutive_failures", 0)) + 1
    health["consecutive_failures"] = streak
    health["last_failure"] = now_display().strftime("%Y-%m-%d %H:%M")
    health["last_error"] = error[:2000]
    save_ticket_health(health)
    return streak


def maybe_alert_ticket_failure_streak(streak: int, error: str = "") -> None:
    if streak < TICKET_FAIL_ALERT_THRESHOLD:
        return
    if streak % TICKET_FAIL_ALERT_THRESHOLD != 0:
        return
    if CI_MODE:
        log.warning(
            "Ticket query failed %s times in a row (CI; email alert skipped): %s",
            streak,
            error or "unknown",
        )
        return
    notify_ticket_failure_streak(streak, error)


def build_dashboard_sql(year: int, month: int) -> str:
    return DASHBOARD_SQL.replace(_TS_YEAR, str(year)).replace(_TS_MONTH, str(month))


def _execute_team_ticket_sql(sql: str) -> list[dict]:
    from databricks import sql as dbsql

    conn = dbsql.connect(
        server_hostname=os.environ["DATABRICKS_SERVER_HOSTNAME"],
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
    )
    cursor = conn.cursor()
    cursor.execute(sql)
    rows = cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    cursor.close()
    conn.close()
    return [dict(zip(cols, r)) for r in rows]


def _run_team_tickets_once():
    now = now_display()
    return _run_team_tickets_by_member(now.year, now.month)


def _fetch_team_tickets_for_month(year: int, month: int) -> list[dict]:
    ensure_databricks_http_path(SCRIPT_DIR / ".env")
    return normalize_ticket_rows(_run_team_tickets_by_member(year, month))


def _run_summary_queries_once():
    from databricks import sql as dbsql

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

    cursor.execute(DAILY_SQL)
    daily_rows = cursor.fetchall()
    daily_cols = [d[0] for d in cursor.description]

    cursor.close()
    conn.close()

    monthly = [dict(zip(monthly_cols, r)) for r in monthly_rows]
    weekly = [dict(zip(weekly_cols, r)) for r in weekly_rows]
    daily = [dict(zip(daily_cols, r)) for r in daily_rows]
    return monthly, weekly, daily


def _run_ticket_queries_once():
    from databricks import sql as dbsql

    conn = dbsql.connect(
        server_hostname=os.environ["DATABRICKS_SERVER_HOSTNAME"],
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
    )
    cursor = conn.cursor()

    now = now_display()
    sql = validate_ticket_sql(now.year, now.month)
    cursor.execute(sql)
    ticket_rows = cursor.fetchall()
    ticket_cols = [d[0] for d in cursor.description]

    cursor.close()
    conn.close()

    return [dict(zip(ticket_cols, r)) for r in ticket_rows]


def _run_queries_once():
    monthly, weekly = _run_summary_queries_once()
    if os.environ.get("SKIP_TICKETS") == "1":
        log.info("SKIP_TICKETS=1: skipping ticket details query (using cache)")
        return monthly, weekly, []
    tickets = _run_ticket_queries_once()
    return monthly, weekly, tickets


def parse_excel_updated_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", str(raw).strip())
    if not m:
        return None
    year, month, day, hour, minute = (int(part) for part in m.groups())
    return datetime(year, month, day, hour, minute, tzinfo=DISPLAY_TZ)


def parse_dashboard_updated_at(html_path: Path | None = None) -> datetime | None:
    html_path = html_path or (output_dir() / "index.html")
    if not html_path.exists():
        return None
    m = re.search(
        r'Updated:\s*(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})',
        html_path.read_text(encoding="utf-8"),
    )
    if not m:
        return None
    year, month, day, hour, minute = (int(part) for part in m.groups())
    return datetime(year, month, day, hour, minute, tzinfo=DISPLAY_TZ)


def dashboard_is_fresh(max_age_minutes: int) -> bool:
    updated = parse_dashboard_updated_at()
    if updated is None:
        return False
    age_minutes = (now_display() - updated).total_seconds() / 60
    return age_minutes < max_age_minutes


def last_excel_update_at() -> datetime | None:
    return parse_excel_updated_at(load_ticket_store().get(TICKET_META_EXCEL_UPDATED))


def morning_excel_satisfied(now: datetime, last: datetime | None) -> bool:
    if last is None or last.date() != now.date():
        return False
    return TICKET_MORNING_START_HOUR <= last.hour < TICKET_AFTERNOON_HOUR


def afternoon_excel_satisfied(now: datetime, last: datetime | None) -> bool:
    if last is None or last.date() != now.date():
        return False
    return last.hour >= TICKET_AFTERNOON_HOUR


def excel_window_is_fresh(now: datetime | None = None) -> bool:
    """True when today's morning or afternoon Excel target is already met."""
    now = now or now_display()
    last = last_excel_update_at()
    if now.hour < TICKET_AFTERNOON_HOUR:
        return morning_excel_satisfied(now, last)
    return afternoon_excel_satisfied(now, last)


def should_attempt_excel_refresh(now: datetime | None = None) -> bool:
    """Try Excel refresh at 9/17 targets, then catch up on later hourly runs if still pending."""
    if os.environ.get("FORCE_TICKETS") == "1":
        return True
    if os.environ.get("SKIP_TICKETS") == "1":
        return False

    now = now or now_display()
    if now.hour < TICKET_MORNING_START_HOUR or now.hour > DASHBOARD_LAST_HOUR:
        return False

    last = last_excel_update_at()
    if now.hour < TICKET_AFTERNOON_HOUR:
        if morning_excel_satisfied(now, last):
            return False
        log.info("Morning Excel refresh still pending; attempting catch-up")
        return True

    if afternoon_excel_satisfied(now, last):
        return False
    log.info("Afternoon Excel refresh still pending; attempting catch-up")
    return True


def should_refresh_ticket_details(now: datetime | None = None) -> bool:
    """Backward-compatible alias for Excel refresh scheduling."""
    return should_attempt_excel_refresh(now)


def load_ticket_store(tickets_path: Path | None = None) -> dict:
    tickets_path = tickets_path or (output_dir() / "dashboard_tickets.json")
    if not tickets_path.exists():
        return {}
    try:
        store = json.loads(tickets_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return store if isinstance(store, dict) else {}


def load_cached_ticket_details(month_key=None):
    if month_key is None:
        month_key = now_display().strftime("%Y-%m")
    tickets = load_ticket_store().get(month_key, [])
    return tickets if isinstance(tickets, list) else []


def query_team_tickets(*, max_attempts: int = 2, timeout: int | None = None):
    """Fetch team ticket details once; monthly/weekly/daily are derived in Python."""
    ensure_databricks_http_path(SCRIPT_DIR / ".env")

    if not try_acquire_databricks_lock():
        raise RuntimeError("Another Databricks query is already running")

    try:
        for attempt in range(1, max_attempts + 1):
            log.info("Running team tickets query (attempt %s/%s)...", attempt, max_attempts)
            try:
                # Per-employee queries run in-process (~1–2 min). Subprocess + Queue deadlocks
                # when returning 1000+ rows (parent joins before reading the queue).
                if timeout is not None:
                    log.info("Team tickets query (in-process, timeout param ignored)")
                rows = normalize_ticket_rows(_run_team_tickets_once())
                log.info("Got %s team ticket rows", len(rows))
                return rows
            except Exception as e:
                log.warning("Team tickets query attempt %s failed: %s", attempt, e)
                if attempt < max_attempts:
                    log.info("Retrying in 10 seconds...")
                    time.sleep(10)
                else:
                    raise
    finally:
        release_databricks_lock()


def rows_from_summary(monthly, weekly, daily):
    monthly_by_id = {
        canonical_employee_id(row["employee_id"]): row for row in monthly
    }
    rows = []
    for employee_id in EMPLOYEE_IDS:
        summary = monthly_by_id.get(employee_id, {})
        incident_count = int(summary.get("incident_count") or 0)
        task_count = int(summary.get("task_count") or 0)
        rows.append({
            "employee_id": employee_id,
            "employee_name": NAME_OVERRIDES.get(
                employee_id, summary.get("employee_name", employee_id)
            ),
            "incident_count": incident_count,
            "task_count": task_count,
            "total_count": incident_count + task_count,
        })
    rows.sort(key=lambda r: (-r["total_count"], r["employee_id"]))
    rows = apply_name_overrides(rows)

    weekly_rows = []
    for row in weekly:
        employee_id = canonical_employee_id(row["employee_id"])
        if employee_id not in EMPLOYEE_IDS:
            continue
        weekly_rows.append({**row, "employee_id": employee_id})

    daily_rows = []
    for row in daily:
        employee_id = canonical_employee_id(row["employee_id"])
        if employee_id not in EMPLOYEE_IDS:
            continue
        daily_rows.append({**row, "employee_id": employee_id})

    weekly_info = process_weekly_data(weekly_rows, rows)
    daily_info = process_daily_data(daily_rows, rows)
    return rows, weekly_info, daily_info


def summary_rows_from_display_monthly(monthly_display: list[dict]) -> list[dict]:
    """Rebuild employee_id rows from dashboard_history monthly display format."""
    name_to_id = {}
    for employee_id in EMPLOYEE_IDS:
        name_to_id[NAME_OVERRIDES.get(employee_id, employee_id)] = employee_id
        name_to_id[employee_id] = employee_id

    rows = []
    for item in monthly_display:
        employee_name = item.get("employee", "")
        employee_id = name_to_id.get(employee_name)
        if not employee_id:
            continue
        rows.append({
            "employee_id": employee_id,
            "employee_name": employee_name,
            "incident_count": int(item.get("incidents") or 0),
            "task_count": int(item.get("tasks") or 0),
        })
    return rows


def load_cached_dashboard_rows() -> list[dict]:
    """Read last published monthly rows (Excel catch-up must not re-query Databricks)."""
    key = now_display().strftime("%Y-%m")
    candidates = [
        output_dir() / "dashboard_history.json",
        Path(GITHUB_REPO_DIR) / "dashboard_history.json",
        SCRIPT_DIR / "dashboard_history.json",
    ]
    for history_path in candidates:
        if not history_path.exists():
            continue
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
            month_data = history.get(key, {})
            summary_rows = month_data.get("summary_rows")
            if summary_rows:
                return summary_rows
            monthly = month_data.get("monthly")
            if monthly:
                rebuilt = summary_rows_from_display_monthly(monthly)
                if rebuilt:
                    log.info(
                        "Rebuilt %s summary row(s) from dashboard_history monthly display",
                        len(rebuilt),
                    )
                    return rebuilt
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read cached dashboard rows from %s: %s", history_path, exc)
    return []


def fetch_dashboard_from_summary():
    """Fetch chart/summary data via fast summary SQL (Phase 1)."""
    log.info("Fetching dashboard data via summary queries (Phase 1)")
    monthly, weekly, daily = query_summary()
    return rows_from_summary(monthly, weekly, daily)


def refresh_excel_ticket_details(rows) -> bool:
    """Run slow team ticket query after dashboard is published (Phase 2)."""
    log.info(
        "Phase 2: refreshing Excel ticket details after dashboard publish..."
    )
    try:
        ticket_rows = query_team_tickets()
        record_ticket_query_success()
    except Exception as e:
        record_ticket_query_failure(str(e))
        maybe_alert_ticket_failure_streak(
            load_ticket_health()["consecutive_failures"], str(e)
        )
        cached = load_cached_ticket_details()
        log.warning(
            "Excel ticket refresh failed: %s (%s cached tickets kept)",
            e,
            len(cached),
        )
        return False

    ticket_details = process_ticket_details(ticket_rows, rows)
    update_dashboard_tickets(ticket_details)
    log.info(
        "Phase 2 complete: %s ticket details ready for Excel export",
        len(ticket_details),
    )
    return True


_excel_lock_handle = None
_dashboard_lock_handle = None
_databricks_lock_handle = None


def wait_for_dashboard_idle(timeout_sec: int = 900) -> bool:
    """Wait for an in-flight Mac/GHA dashboard refresh to release its lock."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not dashboard_lock_is_held():
            return True
        time.sleep(10)
    return not dashboard_lock_is_held()


def dashboard_lock_is_held() -> bool:
    """True when another process holds the dashboard refresh flock."""
    if not DASHBOARD_LOCK_FILE.exists():
        return False
    try:
        handle = open(DASHBOARD_LOCK_FILE, "r+")
    except OSError:
        return False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
        except OSError:
            return True
    finally:
        handle.close()


def try_acquire_dashboard_lock() -> bool:
    global _dashboard_lock_handle
    try:
        DASHBOARD_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        handle = open(DASHBOARD_LOCK_FILE, "w")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        _dashboard_lock_handle = handle
        return True
    except OSError:
        return False


def release_dashboard_lock() -> None:
    global _dashboard_lock_handle
    if _dashboard_lock_handle is None:
        return
    try:
        fcntl.flock(_dashboard_lock_handle.fileno(), fcntl.LOCK_UN)
        _dashboard_lock_handle.close()
    finally:
        _dashboard_lock_handle = None


def try_acquire_excel_lock() -> bool:
    global _excel_lock_handle
    try:
        EXCEL_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        handle = open(EXCEL_LOCK_FILE, "w")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        _excel_lock_handle = handle
        return True
    except OSError:
        return False


def release_excel_lock() -> None:
    global _excel_lock_handle
    if _excel_lock_handle is None:
        return
    try:
        fcntl.flock(_excel_lock_handle.fileno(), fcntl.LOCK_UN)
        _excel_lock_handle.close()
    finally:
        _excel_lock_handle = None


def try_acquire_databricks_lock() -> bool:
    global _databricks_lock_handle
    try:
        DATABRICKS_QUERY_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        handle = open(DATABRICKS_QUERY_LOCK_FILE, "w")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        _databricks_lock_handle = handle
        return True
    except OSError:
        return False


def release_databricks_lock() -> None:
    global _databricks_lock_handle
    if _databricks_lock_handle is None:
        return
    try:
        fcntl.flock(_databricks_lock_handle.fileno(), fcntl.LOCK_UN)
        _databricks_lock_handle.close()
    finally:
        _databricks_lock_handle = None


def dashboard_html_path() -> Path:
    if CI_MODE:
        return output_dir() / "index.html"
    return SCRIPT_DIR / "dashboard.html"


def spawn_excel_catchup_retries() -> None:
    """Schedule Excel-only retries after short delays (no need to wait for next hour)."""
    if CI_MODE or os.environ.get("EXCEL_CATCHUP_CHILD") == "1":
        return
    if not should_attempt_excel_refresh():
        return

    python = sys.executable
    script = Path(__file__).resolve()
    delay_text = ", ".join(f"{delay}m" for delay in EXCEL_CATCHUP_DELAYS_MIN)
    log.info("Scheduling Excel catch-up retries at +%s", delay_text)
    for delay_min in EXCEL_CATCHUP_DELAYS_MIN:
        cmd = (
            f"sleep {delay_min * 60} && "
            f"EXCEL_CATCHUP_CHILD=1 {python} {script} --excel-only"
        )
        subprocess.Popen(
            ["/bin/bash", "-c", cmd],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def run_excel_catchup(*, skip_if_fresh: bool = False) -> int:
    log.info("=== Excel catch-up started (ci=%s) ===", CI_MODE)
    if skip_if_fresh and CI_MODE and excel_window_is_fresh():
        last = last_excel_update_at()
        log.info(
            "Excel already fresh for current window (updated %s); skipping GHA — Mac likely handled it",
            last.strftime("%Y/%m/%d %H:%M") if last else "unknown",
        )
        return 0
    if not should_attempt_excel_refresh():
        log.info("Excel refresh already satisfied for the current window")
        return 0
    if not try_acquire_excel_lock():
        log.info("Another Excel refresh is already running; skipping catch-up")
        return 0

    if dashboard_lock_is_held():
        log.info("Dashboard refresh in progress; waiting up to 15 min for it to finish...")
        if not wait_for_dashboard_idle(900):
            log.info("Dashboard still running after wait; skipping Excel catch-up")
            return 0

    rows = load_cached_dashboard_rows()
    if not rows:
        log.warning("No cached dashboard rows for Excel catch-up; skipping")
        return 1

    try:
        if refresh_excel_ticket_details(rows):
            push_tickets(required=False)
            log.info("Excel catch-up completed successfully")
            return 0
        log.warning("Excel catch-up failed; cached ticket details kept")
        return 1
    finally:
        release_excel_lock()


def query_summary():
    """Fetch monthly + weekly aggregates. Required for every run."""
    ensure_databricks_http_path(SCRIPT_DIR / ".env")

    if not try_acquire_databricks_lock():
        raise RuntimeError("Another Databricks query is already running")

    try:
        for attempt in range(1, 3):
            log.info(f"Running summary queries (attempt {attempt}/2)...")
            try:
                timeout = summary_timeout_seconds()
                log.info(f"Summary query hard timeout: {timeout}s")
                monthly, weekly, daily = run_with_hard_timeout(
                    "databricks_query_worker:run_summary_queries",
                    timeout,
                    "Databricks summary queries",
                )
                log.info(
                    f"Got {len(monthly)} monthly rows, {len(weekly)} weekly rows, {len(daily)} daily rows"
                )
                return monthly, weekly, daily
            except Exception as e:
                log.warning(f"Summary query attempt {attempt} failed: {e}")
                if attempt < 2:
                    log.info("Retrying in 10 seconds...")
                    time.sleep(10)
                else:
                    raise
    finally:
        release_databricks_lock()


def fetch_tickets_optional():
    """Fetch ticket details. Returns (rows, error). rows is None on skip/failure."""
    if os.environ.get("SKIP_TICKETS") == "1":
        log.info("SKIP_TICKETS=1: skipping ticket details query")
        return None, None

    now = now_display()
    try:
        validate_ticket_sql(now.year, now.month)
    except Exception as e:
        msg = f"Ticket SQL validation failed: {e}"
        log.error(msg)
        return None, msg

    try:
        timeout = ticket_timeout_seconds()
        log.info(f"Running ticket details query (timeout {timeout}s)...")
        tickets = run_with_hard_timeout(
            "databricks_query_worker:run_ticket_queries",
            timeout,
            "Databricks ticket details query",
        )
        log.info(f"Got {len(tickets)} ticket rows")
        return tickets, None
    except Exception as e:
        msg = str(e) or repr(e)
        log.warning(f"Ticket details query failed: {msg}")
        return None, msg


def publish_dashboard(rows, weekly_info, daily_info=None):
    update_dashboard_history(rows, weekly_info, daily_info)
    return update_html(rows, weekly_info, daily_info)


def push_tickets(html_path=None, *, required: bool) -> bool:
    if CI_MODE:
        ok = push_tickets_to_github_ci()
    else:
        ok = push_tickets_to_github()

    if ok:
        return True
    if required:
        log.error("GitHub Pages tickets push failed; Excel page may be stale")
        if CI_MODE:
            sys.exit(1)
    else:
        log.warning("Optional GitHub Pages tickets push failed")
    return False


def push_dashboard(html_path, *, required: bool) -> bool:
    if CI_MODE:
        ok = push_to_github_ci()
    else:
        ok = push_to_github(html_path)

    if ok:
        return True
    if required:
        log.error("GitHub Pages push failed; public dashboard may be stale")
        if CI_MODE:
            sys.exit(1)
    else:
        log.warning("Optional GitHub Pages push failed (ticket details may be local only)")
    return False


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


def process_daily_data(daily_rows, monthly_rows):
    """Build per-employee per-day ticket totals (CST) for the heatmap."""
    today = today_display()
    name_by_id = {r["employee_id"]: r["employee_name"] for r in monthly_rows}
    matrix = {}
    max_val = 1

    for row in daily_rows:
        eid = row["employee_id"]
        name = NAME_OVERRIDES.get(eid, name_by_id.get(eid, eid))
        closed = _to_date(row["closed_date"])
        if not closed:
            continue
        key = closed.isoformat()
        total = int(row.get("total_count") or 0)
        if name not in matrix:
            matrix[name] = {}
        matrix[name][key] = matrix[name].get(key, 0) + total
        if matrix[name][key] > max_val:
            max_val = matrix[name][key]

    for emp in monthly_rows:
        matrix.setdefault(emp["employee_name"], {})

    return {
        "year": today.year,
        "month": today.month,
        "matrix": matrix,
        "maxVal": max_val,
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

    store = load_ticket_store(tickets_path)
    if not store:
        repo_tickets = Path(GITHUB_REPO_DIR) / "dashboard_tickets.json"
        if repo_tickets.exists():
            store = load_ticket_store(repo_tickets)

    store[month_key] = tickets
    store[TICKET_META_EXCEL_UPDATED] = format_updated_time()
    tickets_path.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    log.info(
        "dashboard_tickets.json updated for %s (%s tickets, excel_updated_at=%s)",
        month_key,
        len(tickets),
        store[TICKET_META_EXCEL_UPDATED],
    )
    return tickets_path


MONTH_SEAL_STAMP = SCRIPT_DIR / ".month_seal_stamp"


def fetch_dashboard_for_month(year: int, month: int):
    monthly_rows = build_monthly_rows(_fetch_team_tickets_for_month(year, month))
    return apply_name_overrides(monthly_rows)


def fetch_tickets_for_month(year: int, month: int):
    return _fetch_team_tickets_for_month(year, month)


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


def update_html(rows, weekly_info=None, daily_info=None):
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

    if daily_info:
        daily_js = json.dumps(daily_info, ensure_ascii=False)
        html = re.sub(
            r"let DAILY_DATA = \{.*?\};",
            f"let DAILY_DATA = {daily_js};",
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


def update_dashboard_history(rows, weekly_info=None, daily_info=None):
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
        "summary_rows": [
            {
                "employee_id": r["employee_id"],
                "employee_name": r["employee_name"],
                "incident_count": int(r["incident_count"]),
                "task_count": int(r["task_count"]),
            }
            for r in rows
        ],
        "weekly": weekly_info or prev.get("weekly", {"weeks": [], "data": []}),
        "daily": daily_info or prev.get("daily"),
    }

    history_path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    log.info(f"dashboard_history.json updated for {month_key}")
    return history_path


GITHUB_REPO_DIR = str(Path.home() / ".sn-dashboard-repo")
GITHUB_REPO_URL = "https://github.com/TcxddyLuLu/sn-dashboard.git"
GITHUB_USER = "TcxddyLuLu"

# Tracked in git historically but mutated during CI runs; never commit these.
CI_RUNTIME_GIT_PATHS = (
    "automation/dashboard.log",
    "automation/ticket_query_health.json",
)

REPO_GITIGNORE = """.vercel
.env*
automation/dashboard.log
automation/ticket_query_health.json
automation/__pycache__/
automation/.dashboard_refresh.lock
automation/.excel_refresh.lock
automation/.databricks_query.lock
**/__pycache__/
"""

DASHBOARD_STATIC_FILES = [
    "chart.min.js",
    "dashboard-features.js",
    "dashboard_history.json",
    "tickets.html",
    "tickets-features.js",
]

AUTOMATION_SYNC_FILES = [
    "run_dashboard.py",
    "databricks_connect.py",
    "databricks_query_worker.py",
    "dashboard_alerts.py",
    "requirements-ci.txt",
    "dashboard_query.sql",
    "weekly_query.sql",
    "daily_query.sql",
    "ticket_details_query.sql",
    "team_incidents_query.sql",
    "team_tasks_query.sql",
    "team_member_incident.sql",
    "team_member_task.sql",
]

TICKETS_STATIC_FILES = [
    "tickets.html",
    "tickets-features.js",
    "dashboard_tickets.json",
    "dashboard_history.json",
    "xlsx.full.min.js",
]


def ensure_repo_gitignore(repo_dir: Path) -> None:
    path = repo_dir / ".gitignore"
    if not path.exists() or path.read_text(encoding="utf-8") != REPO_GITIGNORE:
        path.write_text(REPO_GITIGNORE, encoding="utf-8")


def untrack_ci_runtime_files(repo_dir: Path) -> None:
    subprocess.run(
        [
            "git", "-C", str(repo_dir), "rm", "-r", "--cached", "--ignore-unmatch",
            *CI_RUNTIME_GIT_PATHS,
            "automation/__pycache__",
        ],
        capture_output=True,
    )


def sync_automation_to_repo(repo_dir: Path) -> None:
    """Keep GHA automation/ in sync with the Mac source scripts."""
    dest = repo_dir / "automation"
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for fname in AUTOMATION_SYNC_FILES:
        src = SCRIPT_DIR / fname
        if not src.exists():
            continue
        shutil.copy2(str(src), str(dest / fname))
        copied += 1
    if copied:
        log.info("Synced %s file(s) to repo automation/", copied)


def _clean_ci_git_noise(repo_dir: Path) -> None:
    """Discard CI-local edits to tracked runtime files before pull/rebase."""
    if CI_RUNTIME_GIT_PATHS:
        subprocess.run(
            ["git", "-C", str(repo_dir), "restore", "--", *CI_RUNTIME_GIT_PATHS],
            capture_output=True,
        )
    subprocess.run(
        ["git", "-C", str(repo_dir), "clean", "-fd", "--", "automation/__pycache__"],
        capture_output=True,
    )


def _ci_git_pull_with_autostash(repo_dir: Path, *, attempts: int = 3) -> bool:
    _clean_ci_git_noise(repo_dir)
    for attempt in range(1, attempts + 1):
        try:
            pull = subprocess.run(
                ["git", "-C", str(repo_dir), "pull", "--rebase", "--autostash"],
                capture_output=True,
                text=True,
                timeout=GIT_NETWORK_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            log.warning("CI git pull attempt %s/%s timed out after %ss", attempt, attempts, GIT_NETWORK_TIMEOUT_SEC)
            pull = None
        if pull is not None and pull.returncode == 0:
            return True
        if pull is not None:
            log.warning("CI git pull attempt %s/%s failed: %s", attempt, attempts, pull.stderr.strip())
        if attempt < attempts:
            time.sleep(5)
    return False


def _recover_git_repo(repo_dir: Path, env: dict | None = None) -> None:
    """Clear unfinished rebase/merge states that block pull --rebase --autostash."""
    rd = str(repo_dir)
    git_dir = repo_dir / ".git"
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        log.warning("Detected unfinished git rebase; aborting before pull")
        _git_run(repo_dir, ["rebase", "--abort"], env=env, timeout=30)
    if (git_dir / "MERGE_HEAD").exists():
        log.warning("Detected unfinished git merge; aborting before pull")
        _git_run(repo_dir, ["merge", "--abort"], env=env, timeout=30)

    conflicts = _git_run(repo_dir, ["diff", "--name-only", "--diff-filter=U"], env=env, timeout=30)
    if conflicts.returncode == 0 and conflicts.stdout.strip():
        log.warning("Unresolved merge conflicts detected; resetting repo to origin/main")
        _git_run(repo_dir, ["fetch", "origin"], env=env)
        _git_run(repo_dir, ["reset", "--hard", "origin/main"], env=env, timeout=30)


def _git_run(repo_dir, args, env=None, *, timeout: int = GIT_NETWORK_TIMEOUT_SEC):
    try:
        return subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        log.warning("Git %s timed out after %ss", " ".join(args[:2]), timeout)
        return subprocess.CompletedProcess(
            args=["git", *args],
            returncode=124,
            stdout=exc.stdout or "",
            stderr=(exc.stderr or "") + f"\nTimed out after {timeout}s",
        )


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
    _recover_git_repo(repo_dir, env=env)

    pull_result = None
    for attempt in range(1, 4):
        pull_result = _git_run(repo_dir, ["pull", "--rebase", "--autostash"], env=env)
        if pull_result.returncode == 0:
            break
        stderr = (pull_result.stderr or "").lower()
        if "未合并" in pull_result.stderr or "unmerged" in stderr or "conflict" in stderr:
            log.warning("Git pull blocked by merge state; attempting repo recovery")
            _recover_git_repo(repo_dir, env=env)
        log.warning(f"Git pull attempt {attempt}/3 failed: {pull_result.stderr.strip()}")
        if attempt < 3:
            time.sleep(5)
    if pull_result is None or pull_result.returncode != 0:
        log.warning(f"Git pull failed: {pull_result.stderr if pull_result else 'unknown'}")
        notify_push_failure("SN Dashboard", "git pull", pull_result.stderr if pull_result else "")
        return False

    shutil.copy2(str(html_path), str(repo_dir / "index.html"))

    for fname in DASHBOARD_STATIC_FILES:
        src = SCRIPT_DIR / fname
        if src.exists():
            shutil.copy2(str(src), str(repo_dir / fname))

    sync_automation_to_repo(repo_dir)
    ensure_repo_gitignore(repo_dir)
    untrack_ci_runtime_files(repo_dir)

    subprocess.run(
        ["git", "-C", str(repo_dir), "add", "index.html", *DASHBOARD_STATIC_FILES, "automation", ".gitignore"],
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

    push_result = _git_run(repo_dir, ["push"], env=env)
    if push_result.returncode == 0:
        log.info("GitHub Pages updated successfully")
        return True

    if "rejected" in (push_result.stderr or "").lower():
        log.warning("Push rejected, retrying after pull --rebase --autostash")
        retry_pull = _git_run(repo_dir, ["pull", "--rebase", "--autostash"], env=env)
        if retry_pull.returncode == 0:
            push_result = _git_run(repo_dir, ["push"], env=env)
            if push_result.returncode == 0:
                log.info("GitHub Pages updated successfully (after retry)")
                return True

    log.warning(f"GitHub push failed: {push_result.stderr}")
    notify_push_failure("SN Dashboard", "git push", push_result.stderr)
    return False


def push_to_github_ci() -> bool:
    repo_dir = output_dir()
    for fname in DASHBOARD_STATIC_FILES:
        src = SCRIPT_DIR / fname
        if src.exists() and src.parent != repo_dir:
            shutil.copy2(str(src), str(repo_dir / fname))

    subprocess.run(
        ["git", "config", "user.name", "github-actions[bot]"],
        cwd=repo_dir, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
        cwd=repo_dir, check=True,
    )

    _clean_ci_git_noise(repo_dir)

    files = ["index.html", "dashboard_history.json", *DASHBOARD_STATIC_FILES]
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

    for attempt in range(1, 4):
        if _ci_git_pull_with_autostash(repo_dir):
            break
        if attempt < 3:
            time.sleep(5)
    else:
        log.error("CI git pull failed after retries")
        return False

    try:
        push = subprocess.run(
            ["git", "push"], cwd=repo_dir, capture_output=True, text=True,
            timeout=GIT_NETWORK_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        log.error("CI git push timed out after %ss", GIT_NETWORK_TIMEOUT_SEC)
        return False
    if push.returncode == 0:
        log.info("GitHub Pages updated successfully")
        return True

    if "rejected" in (push.stderr or "").lower():
        log.warning("CI push rejected, retrying after pull --rebase --autostash")
        if _ci_git_pull_with_autostash(repo_dir):
            try:
                push = subprocess.run(
                    ["git", "push"], cwd=repo_dir, capture_output=True, text=True,
                    timeout=GIT_NETWORK_TIMEOUT_SEC,
                )
            except subprocess.TimeoutExpired:
                log.error("CI git push timed out after %ss", GIT_NETWORK_TIMEOUT_SEC)
                return False
            if push.returncode == 0:
                log.info("GitHub Pages updated successfully (after retry)")
                return True

    log.error("CI git push failed: %s", push.stderr)
    return False


def push_tickets_to_github() -> bool:
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
            notify_push_failure("SN Dashboard Tickets", "git clone", result.stderr)
            return False

    _ensure_git_config(repo_dir)
    _recover_git_repo(repo_dir, env=env)

    pull_result = None
    for attempt in range(1, 4):
        pull_result = _git_run(repo_dir, ["pull", "--rebase", "--autostash"], env=env)
        if pull_result.returncode == 0:
            break
        stderr = (pull_result.stderr or "").lower()
        if "未合并" in pull_result.stderr or "unmerged" in stderr or "conflict" in stderr:
            log.warning("Git pull blocked by merge state; attempting repo recovery")
            _recover_git_repo(repo_dir, env=env)
        log.warning(f"Git pull attempt {attempt}/3 failed: {pull_result.stderr.strip()}")
        if attempt < 3:
            time.sleep(5)
    if pull_result is None or pull_result.returncode != 0:
        log.warning(f"Git pull failed: {pull_result.stderr if pull_result else 'unknown'}")
        notify_push_failure("SN Dashboard Tickets", "git pull", pull_result.stderr if pull_result else "")
        return False

    for fname in TICKETS_STATIC_FILES:
        src = SCRIPT_DIR / fname
        if src.exists():
            shutil.copy2(str(src), str(repo_dir / fname))
        elif fname == "dashboard_history.json":
            hist = output_dir() / "dashboard_history.json"
            if hist.exists():
                shutil.copy2(str(hist), str(repo_dir / fname))

    subprocess.run(
        ["git", "-C", str(repo_dir), "add", *TICKETS_STATIC_FILES],
        capture_output=True,
    )

    diff_result = subprocess.run(
        ["git", "-C", str(repo_dir), "diff", "--cached", "--quiet"],
        capture_output=True,
    )
    if diff_result.returncode == 0:
        log.info("GitHub Pages tickets: no changes to push")
        return True

    now_str = now_display().strftime("%Y-%m-%d %H:%M")
    commit_result = subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-m", f"Update Excel tickets {now_str} CST"],
        capture_output=True, text=True, env=env,
    )
    if commit_result.returncode != 0:
        log.warning(f"Git commit failed: {commit_result.stderr}")
        notify_push_failure("SN Dashboard Tickets", "git commit", commit_result.stderr)
        return False

    push_result = _git_run(repo_dir, ["push"], env=env)
    if push_result.returncode == 0:
        log.info("GitHub Pages tickets updated successfully")
        return True

    if "rejected" in (push_result.stderr or "").lower():
        log.warning("Tickets push rejected, retrying after pull --rebase --autostash")
        retry_pull = _git_run(repo_dir, ["pull", "--rebase", "--autostash"], env=env)
        if retry_pull.returncode == 0:
            push_result = _git_run(repo_dir, ["push"], env=env)
            if push_result.returncode == 0:
                log.info("GitHub Pages tickets updated successfully (after retry)")
                return True

    log.warning(f"GitHub tickets push failed: {push_result.stderr}")
    notify_push_failure("SN Dashboard Tickets", "git push", push_result.stderr)
    return False


def push_tickets_to_github_ci() -> bool:
    repo_dir = output_dir()
    for fname in TICKETS_STATIC_FILES:
        src = SCRIPT_DIR / fname
        if src.exists() and src.parent != repo_dir:
            shutil.copy2(str(src), str(repo_dir / fname))
        elif fname == "dashboard_history.json":
            hist = repo_dir / "dashboard_history.json"
            if not hist.exists():
                alt = SCRIPT_DIR / fname
                if alt.exists():
                    shutil.copy2(str(alt), str(hist))

    subprocess.run(
        ["git", "config", "user.name", "github-actions[bot]"],
        cwd=repo_dir, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
        cwd=repo_dir, check=True,
    )

    _clean_ci_git_noise(repo_dir)

    files = list(TICKETS_STATIC_FILES)
    subprocess.run(["git", "add", *files], cwd=repo_dir, check=True)

    if subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=repo_dir
    ).returncode == 0:
        log.info("GitHub Pages tickets: no changes to push")
        return True

    now_str = now_display().strftime("%Y-%m-%d %H:%M")
    subprocess.run(
        ["git", "commit", "-m", f"Update Excel tickets {now_str} CST"],
        cwd=repo_dir, check=True,
    )

    for attempt in range(1, 4):
        if _ci_git_pull_with_autostash(repo_dir):
            break
        if attempt < 3:
            time.sleep(5)
    else:
        log.error("CI tickets git pull failed after retries")
        return False

    try:
        push = subprocess.run(
            ["git", "push"], cwd=repo_dir, capture_output=True, text=True,
            timeout=GIT_NETWORK_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        log.error("CI tickets git push timed out after %ss", GIT_NETWORK_TIMEOUT_SEC)
        return False
    if push.returncode == 0:
        log.info("GitHub Pages tickets updated successfully")
        return True

    if "rejected" in (push.stderr or "").lower():
        log.warning("CI tickets push rejected, retrying after pull --rebase --autostash")
        if _ci_git_pull_with_autostash(repo_dir):
            try:
                push = subprocess.run(
                    ["git", "push"], cwd=repo_dir, capture_output=True, text=True,
                    timeout=GIT_NETWORK_TIMEOUT_SEC,
                )
            except subprocess.TimeoutExpired:
                log.error("CI tickets git push timed out after %ss", GIT_NETWORK_TIMEOUT_SEC)
                return False
            if push.returncode == 0:
                log.info("GitHub Pages tickets updated successfully (after retry)")
                return True

    log.error("CI tickets git push failed: %s", push.stderr)
    return False


def main():
    global CI_MODE
    parser = argparse.ArgumentParser(description="SN Dashboard automation")
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI mode: commit updated dashboard to GITHUB_WORKSPACE",
    )
    parser.add_argument(
        "--excel-only",
        action="store_true",
        help="Excel catch-up only: skip dashboard publish, retry ticket details",
    )
    parser.add_argument(
        "--skip-if-fresh",
        nargs="?",
        const=-1,
        type=int,
        metavar="MINUTES",
        help="CI skip if fresh (excel-only: morning/afternoon window; charts: index.html age in minutes)",
    )
    args = parser.parse_args()
    CI_MODE = args.ci or os.environ.get("CI", "").lower() == "true"

    if args.excel_only:
        skip_excel = CI_MODE and args.skip_if_fresh is not None
        sys.exit(run_excel_catchup(skip_if_fresh=skip_excel))

    now = now_display()
    now_str = now.strftime("%Y-%m-%d %H:%M")

    log.info(f"=== Dashboard automation started at {now_str} (ci={CI_MODE}) ===")

    if not try_acquire_dashboard_lock():
        log.info("Another dashboard refresh is already running; exiting")
        sys.exit(0 if CI_MODE else 1)

    exit_code = 0
    html_path = None
    try:
        if (
            CI_MODE
            and args.skip_if_fresh is not None
            and args.skip_if_fresh >= 0
            and dashboard_is_fresh(args.skip_if_fresh)
        ):
            updated = parse_dashboard_updated_at()
            log.info(
                "Dashboard already fresh (updated %s); skipping GHA run — Mac likely handled it",
                updated.strftime("%Y/%m/%d %H:%M") if updated else "recently",
            )
        else:
            seal_previous_month_if_needed()

            try:
                rows, weekly_info, daily_info = fetch_dashboard_from_summary()
            except Exception as e:
                log.error(f"Databricks query failed: {e}")
                record_ticket_query_failure(str(e))
                maybe_alert_ticket_failure_streak(load_ticket_health()["consecutive_failures"], str(e))
                notify_failure_safe("SN Dashboard", e)
                exit_code = 1
            else:
                html_path = publish_dashboard(rows, weekly_info, daily_info)

                days_left = (TOKEN_EXPIRY - today_display()).days
                if days_left <= TOKEN_WARN_DAYS:
                    log.warning(
                        "Databricks token expires in %s days (%s); renew before automation stops",
                        days_left,
                        TOKEN_EXPIRY,
                    )
    finally:
        release_dashboard_lock()

    if html_path is not None:
        if not push_dashboard(html_path, required=True):
            exit_code = 1
        else:
            log.info("Dashboard charts published (Excel updates separately on tickets.html)")
            log.info("=== Dashboard automation completed ===")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
