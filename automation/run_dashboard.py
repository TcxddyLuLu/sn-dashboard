#!/usr/bin/env python3
"""
Daily Dashboard Automation
- Queries Databricks for monthly completed ticket counts
- Updates dashboard.html with fresh data
- On the 1st of each month, seals the previous month (summary + ticket details)
- Pushes to GitHub Pages
"""

import os, sys, json, re, subprocess, logging, shutil, argparse
from pathlib import Path
from datetime import datetime, date, timedelta
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
TICKET_FAIL_ALERT_THRESHOLD = 3
TICKET_HEALTH_FILE = SCRIPT_DIR / "ticket_query_health.json"
TICKET_META_EXCEL_UPDATED = "_excel_updated_at"
TICKET_MORNING_START_HOUR = 9
TICKET_AFTERNOON_HOUR = 17
DASHBOARD_LAST_HOUR = 17
SQL_PLACEHOLDER_RE = re.compile(r"__MONTH_[A-Z_]+__|\{_MONTH_[A-Z_]+\}")

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
    'HZh8','PZh105',
]

DASHBOARD_SQL = (SCRIPT_DIR / "dashboard_query.sql").read_text()
WEEKLY_SQL = (SCRIPT_DIR / "weekly_query.sql").read_text()
DAILY_SQL = (SCRIPT_DIR / "daily_query.sql").read_text()
TICKET_DETAILS_SQL = (SCRIPT_DIR / "ticket_details_query.sql").read_text()
TEAM_TICKETS_SQL = (SCRIPT_DIR / "team_tickets_query.sql").read_text()
_TS_YEAR = "YEAR(from_utc_timestamp(CURRENT_TIMESTAMP(), 'Asia/Shanghai'))"
_TS_MONTH = "MONTH(from_utc_timestamp(CURRENT_TIMESTAMP(), 'Asia/Shanghai'))"
_MONTH_START_PH = "__MONTH_START__"
_MONTH_END_PH = "__MONTH_END__"
_MONTH_START_DATE_PH = "__MONTH_START_DATE__"
_MONTH_END_DATE_PH = "__MONTH_END_DATE__"
_UPPER_EMPLOYEE_IDS_PH = "__UPPER_EMPLOYEE_IDS__"

QUERY_TIMEOUT_SUMMARY_LOCAL = 300
QUERY_TIMEOUT_SUMMARY_CI = 180
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


def build_team_tickets_sql(year: int, month: int) -> str:
    month_start, month_end = month_date_range(year, month)
    upper_ids = ", ".join(f"'{employee_id.upper()}'" for employee_id in EMPLOYEE_IDS)
    return (
        TEAM_TICKETS_SQL.replace(_UPPER_EMPLOYEE_IDS_PH, upper_ids)
        .replace(_MONTH_START_DATE_PH, f"'{month_start}'")
        .replace(_MONTH_END_DATE_PH, f"'{month_end}'")
    )


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


def _run_team_tickets_once():
    from databricks import sql as dbsql

    now = now_display()
    sql = build_team_tickets_sql(now.year, now.month)
    conn = dbsql.connect(
        server_hostname=os.environ["DATABRICKS_SERVER_HOSTNAME"],
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
    )
    cursor = conn.cursor()
    cursor.execute(sql)
    ticket_rows = cursor.fetchall()
    ticket_cols = [d[0] for d in cursor.description]
    cursor.close()
    conn.close()
    return [dict(zip(ticket_cols, r)) for r in ticket_rows]


def _fetch_team_tickets_for_month(year: int, month: int) -> list[dict]:
    from databricks import sql as dbsql

    ensure_databricks_http_path(SCRIPT_DIR / ".env")
    sql = build_team_tickets_sql(year, month)
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
    return normalize_ticket_rows([dict(zip(cols, r)) for r in rows])


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

    for attempt in range(1, max_attempts + 1):
        log.info("Running team tickets query (attempt %s/%s)...", attempt, max_attempts)
        try:
            query_timeout = timeout if timeout is not None else team_query_timeout_seconds()
            log.info("Team tickets query hard timeout: %ss", query_timeout)
            rows = run_with_hard_timeout(
                "databricks_query_worker:run_team_tickets_query",
                query_timeout,
                "Databricks team tickets query",
            )
            rows = normalize_ticket_rows(rows)
            log.info("Got %s team ticket rows", len(rows))
            return rows
        except Exception as e:
            log.warning("Team tickets query attempt %s failed: %s", attempt, e)
            if attempt < max_attempts:
                log.info("Retrying in 10 seconds...")
                import time
                time.sleep(10)
            else:
                raise


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


def query_summary():
    """Fetch monthly + weekly aggregates. Required for every run."""
    ensure_databricks_http_path(SCRIPT_DIR / ".env")

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
                import time
                time.sleep(10)
            else:
                raise


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
        "weekly": weekly_info or prev.get("weekly", {"weeks": [], "data": []}),
        "daily": daily_info or prev.get("daily"),
    }

    history_path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    log.info(f"dashboard_history.json updated for {month_key}")
    return history_path


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

    pull_result = None
    for attempt in range(1, 4):
        pull_result = subprocess.run(
            ["git", "-C", str(repo_dir), "pull", "--rebase", "--autostash"],
            env=env, capture_output=True, text=True,
        )
        if pull_result.returncode == 0:
            break
        log.warning(f"Git pull attempt {attempt}/3 failed: {pull_result.stderr.strip()}")
        if attempt < 3:
            import time
            time.sleep(5)
    if pull_result is None or pull_result.returncode != 0:
        log.warning(f"Git pull failed: {pull_result.stderr if pull_result else 'unknown'}")
        notify_push_failure("SN Dashboard", "git pull", pull_result.stderr if pull_result else "")
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

    for attempt in range(1, 4):
        pull = subprocess.run(
            ["git", "pull", "--rebase"],
            cwd=repo_dir, capture_output=True, text=True,
        )
        if pull.returncode == 0:
            break
        log.warning("CI git pull attempt %s/3 failed: %s", attempt, pull.stderr.strip())
        if attempt < 3:
            import time
            time.sleep(5)
    else:
        log.error("CI git pull failed after retries")
        return False

    push = subprocess.run(["git", "push"], cwd=repo_dir, capture_output=True, text=True)
    if push.returncode == 0:
        log.info("GitHub Pages updated successfully")
        return True

    if "rejected" in (push.stderr or "").lower():
        log.warning("CI push rejected, retrying after pull --rebase")
        if subprocess.run(["git", "pull", "--rebase"], cwd=repo_dir).returncode == 0:
            push = subprocess.run(["git", "push"], cwd=repo_dir, capture_output=True, text=True)
            if push.returncode == 0:
                log.info("GitHub Pages updated successfully (after retry)")
                return True

    log.error("CI git push failed: %s", push.stderr)
    return False


def main():
    global CI_MODE
    parser = argparse.ArgumentParser(description="SN Dashboard automation")
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI mode: commit updated dashboard to GITHUB_WORKSPACE",
    )
    args = parser.parse_args()
    CI_MODE = args.ci or os.environ.get("CI", "").lower() == "true"

    now = now_display()
    now_str = now.strftime("%Y-%m-%d %H:%M")

    log.info(f"=== Dashboard automation started at {now_str} (ci={CI_MODE}) ===")

    seal_previous_month_if_needed()

    try:
        rows, weekly_info, daily_info = fetch_dashboard_from_summary()
    except Exception as e:
        log.error(f"Databricks query failed: {e}")
        record_ticket_query_failure(str(e))
        maybe_alert_ticket_failure_streak(load_ticket_health()["consecutive_failures"], str(e))
        notify_failure_safe("SN Dashboard", e)
        sys.exit(1)

    html_path = publish_dashboard(rows, weekly_info, daily_info)

    days_left = (TOKEN_EXPIRY - today_display()).days
    if days_left <= TOKEN_WARN_DAYS:
        log.warning(
            "Databricks token expires in %s days (%s); renew before automation stops",
            days_left,
            TOKEN_EXPIRY,
        )

    push_dashboard(html_path, required=True)
    log.info("Phase 1 complete: chart/summary data published")

    if should_attempt_excel_refresh():
        if refresh_excel_ticket_details(rows):
            push_dashboard(html_path, required=False)
    else:
        cached = load_cached_ticket_details()
        last = last_excel_update_at()
        log.info(
            "Excel ticket file unchanged (%s cached tickets; last excel update: %s)",
            len(cached),
            last.strftime("%Y/%m/%d %H:%M") if last else "never",
        )

    log.info("=== Dashboard automation completed ===")


if __name__ == "__main__":
    main()
