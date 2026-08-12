"""Auto-resolve Databricks SQL warehouse path when the configured endpoint changes."""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import re
import urllib.error
import urllib.request
import importlib
from pathlib import Path
from typing import Callable, TypeVar, Union

log = logging.getLogger(__name__)

T = TypeVar("T")
CallableTarget = Union[str, Callable[[], T]]

WAREHOUSE_NAME_HINTS = ("servicenow", "rese_ops_health")


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _warehouse_path(warehouse_id: str) -> str:
    return f"/sql/1.0/warehouses/{warehouse_id}"


def list_warehouses(hostname: str, token: str) -> list[dict]:
    req = urllib.request.Request(
        f"https://{hostname}/api/2.0/sql/warehouses",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("warehouses", [])


def test_connection(hostname: str, http_path: str, token: str) -> None:
    from databricks import sql as dbsql

    conn = dbsql.connect(
        server_hostname=hostname,
        http_path=http_path,
        access_token=token,
    )
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        cursor.close()
    finally:
        conn.close()


def _score_warehouse(warehouse: dict) -> int:
    name = (warehouse.get("name") or "").lower()
    score = 0
    if warehouse.get("state") == "RUNNING":
        score += 10
    for hint in WAREHOUSE_NAME_HINTS:
        if hint in name:
            score += 20
    return score


def discover_http_path(hostname: str, token: str) -> tuple[str, str]:
    warehouses = sorted(list_warehouses(hostname, token), key=_score_warehouse, reverse=True)
    if not warehouses:
        raise RuntimeError("No SQL warehouses returned by Databricks API")

    errors: list[str] = []
    for warehouse in warehouses:
        path = _warehouse_path(warehouse["id"])
        name = warehouse.get("name") or warehouse["id"]
        try:
            test_connection(hostname, path, token)
            return path, name
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    raise RuntimeError(
        "No accessible SQL warehouse found. Tried: " + "; ".join(errors[:3])
    )


def _update_env_file(env_file: Path, http_path: str) -> None:
    text = env_file.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r"^DATABRICKS_HTTP_PATH=.*$",
        f"DATABRICKS_HTTP_PATH={http_path}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count == 0:
        new_text = text.rstrip() + f"\nDATABRICKS_HTTP_PATH={http_path}\n"
    env_file.write_text(new_text, encoding="utf-8")


def ensure_databricks_http_path(env_file: Path | None = None) -> str:
    """Use configured warehouse when possible; otherwise auto-discover and persist."""
    hostname = _require_env("DATABRICKS_SERVER_HOSTNAME")
    token = _require_env("DATABRICKS_TOKEN")
    current = _require_env("DATABRICKS_HTTP_PATH")
    env_file = env_file or Path(__file__).resolve().parent / ".env"

    try:
        test_connection(hostname, current, token)
        return current
    except Exception as exc:
        log.warning("Configured Databricks warehouse unavailable: %s", exc)

    path, name = discover_http_path(hostname, token)
    if path != current:
        log.info("Auto-switched Databricks warehouse to %s (%s)", name, path)
        if env_file.exists():
            _update_env_file(env_file, path)
    os.environ["DATABRICKS_HTTP_PATH"] = path
    return path


def _resolve_callable(target: CallableTarget) -> Callable[[], T]:
    if callable(target):
        module = target.__module__
        name = target.__qualname__
        if module == "__main__":
            raise RuntimeError(
                "Hard-timeout queries must use databricks_query_worker entrypoints "
                "(e.g. 'databricks_query_worker:run_dashboard_queries')"
            )
        target = f"{module}:{name}"
    module_name, func_name = target.split(":", 1)
    module = importlib.import_module(module_name)
    return getattr(module, func_name)


def _hard_timeout_worker(target: CallableTarget, result_queue: mp.Queue) -> None:
    try:
        func = _resolve_callable(target)
        result_queue.put(("ok", func()))
    except Exception as exc:
        result_queue.put(("err", exc))


def run_with_hard_timeout(target: CallableTarget, timeout_seconds: int, label: str) -> T:
    """Run a worker entrypoint in a child process; kill it after timeout_seconds."""
    ctx = mp.get_context("spawn")
    result_queue = ctx.Queue()
    proc = ctx.Process(
        target=_hard_timeout_worker,
        args=(target, result_queue),
        daemon=True,
    )
    proc.start()
    proc.join(timeout_seconds)

    if proc.is_alive():
        log.error("%s exceeded %ss hard timeout; terminating worker", label, timeout_seconds)
        proc.terminate()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
            proc.join(5)
        raise TimeoutError(f"{label} exceeded {timeout_seconds}s hard timeout")

    if result_queue.empty():
        raise RuntimeError(f"{label} finished without returning a result")

    status, payload = result_queue.get_nowait()
    if status == "err":
        raise payload
    return payload
