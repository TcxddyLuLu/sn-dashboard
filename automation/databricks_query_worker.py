"""Databricks query entrypoints for child-process hard timeouts."""

from pathlib import Path

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent


def _load_env() -> None:
    load_dotenv(SCRIPT_DIR / ".env")


def run_dashboard_queries():
    _load_env()
    from run_dashboard import _run_queries_once

    return _run_queries_once()


def run_team_tickets_query():
    _load_env()
    from run_dashboard import _run_team_tickets_once

    return _run_team_tickets_once()


def run_summary_queries():
    _load_env()
    from run_dashboard import _run_summary_queries_once

    return _run_summary_queries_once()


def run_monthly_query():
    _load_env()
    from run_dashboard import _run_monthly_query_once

    return _run_monthly_query_once()


def run_weekly_query():
    _load_env()
    from run_dashboard import _run_weekly_query_once

    return _run_weekly_query_once()


def run_daily_query():
    _load_env()
    from run_dashboard import _run_daily_query_once

    return _run_daily_query_once()


def run_ticket_queries():
    _load_env()
    from run_dashboard import _run_ticket_queries_once

    return _run_ticket_queries_once()
