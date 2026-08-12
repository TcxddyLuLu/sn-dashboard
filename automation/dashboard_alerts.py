"""Email alerts when dashboard automation fails."""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime

log = logging.getLogger(__name__)


def send_text_email(subject: str, body: str, recipient: str) -> bool:
    log.info("Sending alert email to %s...", recipient)
    safe_subject = subject.replace('"', '\\"')
    safe_body = body.replace('"', '\\"').replace("\n", "\\n")
    ascript = f'''
    tell application "Microsoft Outlook"
        set newMsg to make new outgoing message with properties {{subject:"{safe_subject}", content:"{safe_body}"}}
        make new to recipient at newMsg with properties {{email address:{{address:"{recipient}"}}}}
        send newMsg
    end tell
    '''
    result = subprocess.run(["osascript", "-e", ascript], capture_output=True, text=True)
    if result.returncode != 0:
        log.warning("Outlook alert email failed: %s", result.stderr)
        return False
    log.info("Alert email sent via Outlook")
    return True


def notify_failure(job_name: str, error: Exception) -> None:
    recipient = os.environ.get("EMAIL_TO", "luby.lu@nike.com")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    subject = f"[{job_name} FAILED] Data not updated ({now_str})"
    body = (
        f"{job_name} failed at {now_str}.\n\n"
        f"Error: {error}\n\n"
        "The dashboard was not updated. "
        "If this persists, check Databricks token/warehouse access in Cursor."
    )
    send_text_email(subject, body, recipient)


def notify_push_failure(job_name: str, stage: str, detail: str) -> None:
    """Alert when local data updated but GitHub Pages push did not succeed."""
    recipient = os.environ.get("EMAIL_TO", "luby.lu@nike.com")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    subject = f"[{job_name} PUSH FAILED] Website may be stale ({now_str})"
    body = (
        f"{job_name} updated local files at {now_str}, but GitHub Pages was not updated.\n\n"
        f"Stage: {stage}\n"
        f"Detail: {detail.strip() or '(no details)'}\n\n"
        "You may have received an email with fresh data, but the public dashboard "
        "may still show older numbers until this is fixed."
    )
    send_text_email(subject, body, recipient)
