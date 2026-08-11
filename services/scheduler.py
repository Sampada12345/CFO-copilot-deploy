"""
scheduler.py — v3.5

Background scheduler for Gmail reply intake.

Fixes vs v3.4
-------------
* Bug E: `.last_scan` is now written to a path relative to THIS file, not
  the CWD — so the throttle works regardless of where Streamlit was
  launched from.
* Bug F: `misfire_grace_time` bumped to 24h with `coalesce=True` — if the
  PC was asleep at 6 AM, the job now fires when the machine wakes up
  (before it silently skipped past a 1-hour window).
* New: `scheduler_status()` returns next-fire-time + last-run info for the
  Tab 3 job-history panel.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("cfo.scheduler")

# ── Module-level guard so we start the scheduler exactly once. ────────────
_scheduler_started = False
_scheduler_lock = threading.Lock()
_scheduler_ref = None    # holds the BackgroundScheduler so we can inspect it

# ── File-based throttle for on-open scans ─────────────────────────────────
# Fix for Bug E: anchor `.last_scan` to THIS module's directory so the file
# lives with the app regardless of where streamlit was launched from.  We
# previously used Path(".last_scan") which was CWD-relative and broke the
# throttle when the user cd'd elsewhere before starting Streamlit.
_STAMP = Path(__file__).resolve().parent / ".last_scan"
_MIN_INTERVAL_MINUTES = int(os.getenv("SCAN_MIN_INTERVAL_MIN", "60"))
IST = timezone(timedelta(hours=5, minutes=30))


def _run_scan_bg(reason: str) -> None:
    """Run the scan in a fire-and-forget thread. Never raises."""
    def _worker():
        try:
            from ai.ptp_intelligence import poll_gmail_replies
            result = poll_gmail_replies()
            try:
                from services.drive_sync import request_backup
                request_backup()          # back up DBs after the scan writes
            except Exception:
                pass
            _STAMP.write_text(datetime.utcnow().isoformat())
            logger.info("Gmail scan (%s) done: %s", reason, result)
        except Exception as e:
            logger.exception("Gmail scan (%s) failed: %s", reason, e)
    t = threading.Thread(target=_worker, name=f"gmail-scan-{reason}",
                          daemon=True)
    t.start()


def _too_soon() -> bool:
    """True if the last scan was less than _MIN_INTERVAL_MINUTES ago."""
    try:
        last = datetime.fromisoformat(_STAMP.read_text().strip())
    except (FileNotFoundError, ValueError):
        return False
    return datetime.utcnow() - last < timedelta(minutes=_MIN_INTERVAL_MINUTES)


def scan_on_open() -> str:
    """Called at the top of the app on every rerun. Throttled so multiple
    tabs/reruns per hour don't hammer Gmail. Returns a short status string
    the caller can silently log."""
    if _too_soon():
        return "throttled"
    _run_scan_bg(reason="on-open")
    return "started"


def start_daily_scheduler() -> str:
    """Start the daily 06:00 IST scan, ONCE per Python process."""
    global _scheduler_started, _scheduler_ref
    with _scheduler_lock:
        if _scheduler_started:
            return "already-running"
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
        except ImportError:
            logger.warning("APScheduler not installed; daily scan disabled. "
                           "pip install apscheduler")
            return "apscheduler-missing"
        sched = BackgroundScheduler(timezone=IST, daemon=True)
        # ═══════════════════════════════════════════════════════════════════
        #  DAILY RUN TIME — change these two values to shift the schedule.
        #  Currently: 11:00 AM IST.
        #  Examples:
        #    6 AM IST  → hour=6,  minute=0
        #    9:30 AM   → hour=9,  minute=30
        #    2 PM      → hour=14, minute=0
        # ═══════════════════════════════════════════════════════════════════
        DAILY_HOUR   = int(os.getenv("SCAN_DAILY_HOUR",   "11"))
        DAILY_MINUTE = int(os.getenv("SCAN_DAILY_MINUTE", "0"))

        sched.add_job(
            lambda: _run_scan_bg("daily-scheduled"),
            trigger=CronTrigger(hour=DAILY_HOUR, minute=DAILY_MINUTE,
                                 timezone=IST),
            id="daily_gmail_scan",
            replace_existing=True,
            # Fix for Bug F: was 3600 (1h) — if the PC was asleep past 7 AM,
            # the daily scan silently didn't run.  Now 24h + coalesce=True
            # means the missed run fires as soon as the process is alive
            # again, and multiple missed runs collapse into one.
            misfire_grace_time=86400,
            coalesce=True,
        )
        sched.start()
        _scheduler_ref = sched
        _scheduler_started = True
        return "started"


def ensure_scheduler_and_scan() -> dict:
    """One call that both starts the daily scheduler AND triggers an
    on-open scan (throttled).  This is what the dashboard's entrypoint
    imports and calls."""
    return {
        "daily":   start_daily_scheduler(),
        "on_open": scan_on_open(),
    }


def scheduler_status() -> dict:
    """Introspect the running scheduler for the Tab-3 status panel.
    Returns next fire time + last scan timestamp."""
    result = {
        "running": _scheduler_started,
        "next_run": None,
        "last_scan_at": None,
        "stamp_path": str(_STAMP),
    }
    if _scheduler_ref is not None:
        try:
            job = _scheduler_ref.get_job("daily_gmail_scan")
            if job and job.next_run_time:
                result["next_run"] = job.next_run_time.isoformat()
        except Exception:
            pass
    try:
        result["last_scan_at"] = _STAMP.read_text().strip()
    except (FileNotFoundError, ValueError):
        pass
    return result
