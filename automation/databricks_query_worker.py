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
