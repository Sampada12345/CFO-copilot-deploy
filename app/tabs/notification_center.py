"""
reminder_center.py — v3.1
------------------------------------------------------------------
Two things in one file:
  1. Lifecycle engine (deterministic, safe to run every load):
       * ensure_reminder_dates : due_date - REMINDER_BEFORE_DUE_DAYS
                                 (overdue → tomorrow)
       * generate_upcoming_drafts : T-NOTIFY_LEAD_DAYS creates a draft
                                    per-client-template → pending_review
       * autosend_due_drafts : on the send date, dispatch anything that
                               is still pending_review OR reviewed
  2. UI: render_notification_center() drops into Tab 3 to REPLACE the
     old Agent Pipeline Setup — matches the v4 React screen the user
     showed (Sends-in countdown, Client, Invoice, Amount, Template,
     Status chip, Review / Send now / Cancel).

Config (env, matches v4):
  REMINDER_BEFORE_DUE_DAYS  default 3
  NOTIFY_LEAD_DAYS          default 5
"""
from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from core.database import DB_PATH, get_db
from core.template_manager import render_for_client

# ═══════════════════════════════════════════════════════════════════════════
#  BACKGROUND SEND STATE — module-level so the thread and the UI can share it.
#  Kept out of st.session_state on purpose: session_state is per-tab and per-
#  rerun, and would race with the background thread.
# ═══════════════════════════════════════════════════════════════════════════
_send_state = {
    "thread":       None,   # threading.Thread | None
    "stop_event":   None,   # threading.Event | None
    "sent":         0,
    "failed":       0,
    "total":        0,
    "started_at":   None,
    "finished_at":  None,
    "stopped_early": False,
}
_send_lock = threading.Lock()


def _send_state_snapshot() -> dict:
    """Thread-safe read of the current progress."""
    with _send_lock:
        return {k: v for k, v in _send_state.items() if k not in ("thread","stop_event")} | {
            "running": bool(_send_state["thread"] and _send_state["thread"].is_alive()),
            "stop_requested": bool(_send_state["stop_event"] and _send_state["stop_event"].is_set()),
        }


def _reset_send_state():
    with _send_lock:
        _send_state["sent"] = 0
        _send_state["failed"] = 0
        _send_state["total"] = 0
        _send_state["started_at"] = None
        _send_state["finished_at"] = None
        _send_state["stopped_early"] = False


REMINDER_BEFORE_DUE_DAYS = int(os.getenv("REMINDER_BEFORE_DUE_DAYS", "3"))
NOTIFY_LEAD_DAYS         = int(os.getenv("NOTIFY_LEAD_DAYS", "5"))
CC_EMAIL                 = os.getenv("CC_EMAIL", "")

ACTIVE_STATUSES = ("pending", "approved", "pending", "approved")
REMINDABLE_INVOICE_STATUSES = ("unpaid", "overdue", "partial", "promised")


# ═══════════════════════════════════════════════════════════════════════════
#  SCHEMA EVOLUTION
# ═══════════════════════════════════════════════════════════════════════════
def ensure_schema():
    """Add v3.1 columns/statuses without breaking existing v3 rows.
    SQLite allows ADD COLUMN cheaply; wrap in try/except so re-runs are safe.
    Also self-heals any bad date strings left over from earlier crashes."""
    with get_db(DB_PATH) as conn:
        for stmt in [
            "ALTER TABLE invoices ADD COLUMN reminder_date TEXT",
            "ALTER TABLE email_drafts ADD COLUMN scheduled_send_date TEXT",
            "ALTER TABLE email_drafts ADD COLUMN template_used TEXT",
            "ALTER TABLE email_drafts ADD COLUMN reviewed_by TEXT",
            "ALTER TABLE email_drafts ADD COLUMN reviewed_at TEXT",
        ]:
            try:
                conn.execute(stmt); conn.commit()
            except Exception:
                pass  # column already exists — expected on re-runs

        # Self-heal: earlier crashes could have written literal 'NaT' or 'None'
        # into date columns.  Convert those to NULL so downstream code
        # (which correctly handles NULL) doesn't trip.
        for col_table in [("reminder_date",       "invoices"),
                          ("scheduled_send_date", "email_drafts")]:
            col, tbl = col_table
            try:
                conn.execute(
                    f"UPDATE {tbl} SET {col} = NULL "
                    f"WHERE {col} IN ('NaT', 'None', 'nan', '')"
                )
                conn.commit()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
#  LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════════
def _to_date(x):
    """Return a python date or None. Robust to: None, empty string, 'NaT',
    pd.NaT, pandas.Timestamp, datetime, and ISO 'YYYY-MM-DD' strings."""
    if x is None:
        return None
    # pandas NaT
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    # already a date/datetime/Timestamp
    if hasattr(x, "date") and callable(x.date):
        try:
            return x.date()
        except Exception:
            pass
    if isinstance(x, date):
        return x
    s = str(x).strip()
    if s in ("", "NaT", "None", "nan"):
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def ensure_reminder_dates(today: date | None = None) -> int:
    today = today or date.today()
    n = 0
    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, due_date, status FROM invoices "
            "WHERE (reminder_date IS NULL OR reminder_date='') "
            f"  AND status IN ({','.join('?' * len(REMINDABLE_INVOICE_STATUSES))})",
            REMINDABLE_INVOICE_STATUSES,
        ).fetchall()
        for r in rows:
            due = _to_date(r["due_date"])
            if not due: continue
            target = due - timedelta(days=REMINDER_BEFORE_DUE_DAYS)
            if target <= today:
                target = today + timedelta(days=1)
            conn.execute("UPDATE invoices SET reminder_date=? WHERE id=?",
                         (target.isoformat(), r["id"]))
            n += 1
        conn.commit()
    return n


def generate_upcoming_drafts(today: date | None = None) -> int:
    """Create a draft for every invoice whose reminder_date is within the
    T-NOTIFY_LEAD_DAYS window and doesn't already have an active draft."""
    today = today or date.today()
    horizon = (today + timedelta(days=NOTIFY_LEAD_DAYS)).isoformat()
    created = 0
    with get_db(DB_PATH) as conn:
        rows = conn.execute(f"""
            SELECT i.id AS invoice_id, i.invoice_number, i.client_id,
                   i.reminder_date, i.due_date, i.invoice_date, i.total_amount,
                   i.amount, i.currency,
                   c.name AS client_name, c.email AS client_email
            FROM invoices i JOIN clients c ON c.id = i.client_id
            WHERE i.reminder_date IS NOT NULL AND i.reminder_date <= ?
              AND i.status IN ({','.join('?' * len(REMINDABLE_INVOICE_STATUSES))})
        """, (horizon, *REMINDABLE_INVOICE_STATUSES)).fetchall()

        for r in rows:
            # Skip if there's already an ACTIVE draft for this invoice.
            existing = conn.execute(
                "SELECT id FROM email_drafts "
                f"WHERE invoice_id=? AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
                (r["invoice_id"], *ACTIVE_STATUSES),
            ).fetchone()
            if existing: continue

            # v3.5: also skip if we've already SENT a reminder for this
            # invoice within the last REMINDER_COOLDOWN_DAYS days.  This is
            # what prevents the "238 emails sent, 238 pending drafts still
            # appear on next dashboard load" bug — the Excel reload used to
            # re-create drafts for invoices that had been reminded 2 hours
            # ago.  Cooldown default = 5 days; env override REMINDER_COOLDOWN_DAYS.
            cooldown = int(os.getenv("REMINDER_COOLDOWN_DAYS", "5"))
            recently_sent = conn.execute(
                "SELECT id FROM email_drafts "
                "WHERE invoice_id = ? AND status = 'sent' "
                "  AND sent_at IS NOT NULL "
                "  AND datetime(sent_at) > datetime('now', ?)",
                (r["invoice_id"], f"-{cooldown} days"),
            ).fetchone()
            if recently_sent: continue

            if not r["client_email"]: continue

            rendered = render_for_client({
                "client_name":    r["client_name"],
                "client_email":   r["client_email"],
                "invoice_number": r["invoice_number"],
                "amount":         r["total_amount"] or r["amount"] or 0,
                "currency":       r["currency"] or "INR",
                "issue_date":     r["invoice_date"] or "",
                "due_date":       r["due_date"] or "",
            }, today=today)

            conn.execute("""
                INSERT INTO email_drafts (
                    invoice_id, client_id, to_email, cc_email,
                    subject, body, status, scheduled_send_date, template_used
                ) VALUES (?,?,?,?,?,?,?,?,?)
            """, (r["invoice_id"], r["client_id"], r["client_email"], CC_EMAIL,
                  rendered["subject"], rendered["body"], "pending",
                  r["reminder_date"], rendered["template_name"]))
            created += 1
        conn.commit()
    return created


def autosend_due_drafts(send_fn, today: date | None = None,
                         stop_event=None, progress_cb=None,
                         ignore_schedule=False) -> dict:
    """Dispatch active drafts.  send_fn(to, subject, body, cc) → bool.

    By default only sends drafts whose scheduled_send_date has arrived.
    If ignore_schedule=True, sends EVERY active (pending/approved) draft
    regardless of its scheduled date — used by the "Send all pending now"
    button.

    stop_event : a threading.Event.  Checked before EVERY send.  If set,
                 loop exits immediately without dispatching more emails.
    progress_cb: optional callable(sent_so_far:int, failed_so_far:int, total:int)
                 for the UI to display live counters.
    """
    today = today or date.today()
    sent, failed, stopped_early = [], [], False
    with get_db(DB_PATH) as conn:
        if ignore_schedule:
            # Send every active draft, no date filter.
            rows = conn.execute(
                "SELECT * FROM email_drafts "
                f"WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
                (*ACTIVE_STATUSES,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM email_drafts "
                f"WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))}) "
                "  AND scheduled_send_date IS NOT NULL "
                "  AND scheduled_send_date <= ?",
                (*ACTIVE_STATUSES, today.isoformat()),
            ).fetchall()
        total = len(rows)
        for i, d in enumerate(rows):
            # ── cooperative stop check ──────────────────────────────────
            if stop_event is not None and stop_event.is_set():
                stopped_early = True
                break

            try:
                ok = bool(send_fn(to=d["to_email"], subject=d["subject"],
                                  body=d["body"], cc=d["cc_email"] or None))
            except Exception as e:
                ok = False
                conn.execute("UPDATE email_drafts SET status='failed' WHERE id=?",
                             (d["id"],))
                failed.append((d["id"], str(e)))
                if progress_cb:
                    try: progress_cb(len(sent), len(failed), total)
                    except Exception: pass
                continue
            if ok:
                conn.execute(
                    "UPDATE email_drafts SET status='sent', "
                    "sent_at=datetime('now','localtime') WHERE id=?", (d["id"],))
                sent.append(d["id"])
            else:
                conn.execute("UPDATE email_drafts SET status='failed' WHERE id=?",
                             (d["id"],))
                failed.append((d["id"], "send_fn returned False"))
            # Persist after each send so Stop leaves consistent state.
            conn.commit()
            if progress_cb:
                try: progress_cb(len(sent), len(failed), total)
                except Exception: pass
    return {"sent": sent, "failed": failed, "stopped_early": stopped_early,
            "total": total}


def sync_center_with_sent_state(cooldown_days: int | None = None) -> dict:
    """v3.5 — Real-world sync between Notification Center pending drafts and
    what's actually been sent.

    Problem this solves: if you sent 238 drafts last night, then reloaded the
    workbook this morning, the reload logic used to re-create 238 NEW pending
    drafts because the "same invoice, still open" check only looked at ACTIVE
    drafts, not recently-sent ones.

    This function does the cleanup on the DB side: any pending/approved
    draft on an invoice that ALSO has a recently-sent draft is auto-cancelled
    with status='superseded'. Safe to call on every dashboard load.
    """
    ensure_schema()
    cooldown = cooldown_days or int(os.getenv("REMINDER_COOLDOWN_DAYS", "5"))
    with get_db(DB_PATH) as conn:
        # Find invoice_ids where we have BOTH a sent draft (recent) and
        # a pending/approved draft — those pending ones are the ghosts.
        cur = conn.execute(f"""
            SELECT p.id AS pending_id
              FROM email_drafts p
             WHERE p.status IN ({','.join('?' * len(ACTIVE_STATUSES))})
               AND EXISTS (
                   SELECT 1 FROM email_drafts s
                    WHERE s.invoice_id = p.invoice_id
                      AND s.status = 'sent'
                      AND s.sent_at IS NOT NULL
                      AND datetime(s.sent_at) > datetime('now', ?)
                      AND s.id != p.id
               )
        """, (*ACTIVE_STATUSES, f"-{cooldown} days"))
        ghost_ids = [row["pending_id"] for row in cur.fetchall()]

        if ghost_ids:
            placeholders = ",".join("?" * len(ghost_ids))
            conn.execute(
                f"UPDATE email_drafts SET status='rejected' "
                f"WHERE id IN ({placeholders})",
                ghost_ids,
            )
            conn.commit()
    return {"ghost_drafts_cancelled": len(ghost_ids)}


def run_lifecycle(send_fn, today: date | None = None) -> dict:
    ensure_schema()
    sync_center_with_sent_state()   # v3.5 — always sync first
    a = ensure_reminder_dates(today)
    b = generate_upcoming_drafts(today)
    c = autosend_due_drafts(send_fn, today)
    return {"reminder_dates_set": a, "drafts_generated": b, **c}


# ═══════════════════════════════════════════════════════════════════════════
#  UI — Notification Center (Tab 3 replacement)
# ═══════════════════════════════════════════════════════════════════════════
NC_CSS = """
<style>
  .nc-header { display:flex; align-items:center; gap:12px; margin-bottom:6px; }
  .nc-badge {
      background: linear-gradient(135deg,#b8860b,#d4af37);
      color:#0b1f35; font-weight:700; border-radius: 999px;
      padding: 3px 12px; font-size: 13px;
  }
  .chip { padding: 3px 10px; border-radius: 999px; font-size: 12px;
          font-weight: 600; display: inline-block; }
  .chip-today  { background:#fde2e2; color:#c0392b; }
  .chip-soon   { background:#fff3cd; color:#8a6d3b; }
  .chip-later  { background:#d1ecf1; color:#0c5460; }
  .chip-await  { background:#fff3cd; color:#8a6d3b; }
  .chip-review { background:#d4edda; color:#155724; }
</style>
"""


def _load_center(only_active=True) -> pd.DataFrame:
    with get_db(DB_PATH) as conn:
        q = """
        SELECT d.id, d.subject, d.body, d.to_email, d.cc_email,
               d.status, d.scheduled_send_date, d.template_used,
               d.reviewed_by, d.sent_at,
               i.invoice_number, i.total_amount, i.amount, i.currency,
               i.due_date,
               i.original_due_date, i.latest_due_date,
               i.expected_payment_date,
               c.name AS client_name
        FROM email_drafts d
        JOIN invoices i ON i.id = d.invoice_id
        JOIN clients c ON c.id = d.client_id
        """
        if only_active:
            q += f"WHERE d.status IN ({','.join('?' * len(ACTIVE_STATUSES))}) "
            q += "ORDER BY d.scheduled_send_date IS NULL, d.scheduled_send_date"
            rows = conn.execute(q, ACTIVE_STATUSES).fetchall()
        else:
            q += "ORDER BY d.scheduled_send_date DESC LIMIT 200"
            rows = conn.execute(q).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def _days_chip(days: int) -> str:
    if days <= 0:  return f"<span class='chip chip-today'>today</span>"
    if days <= 2:  return f"<span class='chip chip-soon'>{days} day{'s' if days>1 else ''}</span>"
    return f"<span class='chip chip-later'>{days} days</span>"


def _status_chip(status: str) -> str:
    if status in ("pending",):
        return "<span class='chip chip-await'>awaiting review</span>"
    if status in ("approved",):
        return "<span class='chip chip-review'>reviewed · will auto-send</span>"
    return f"<span class='chip'>{status}</span>"


def _s(v, default: str = "—") -> str:
    """Convert any cell value (NaN, None, '', pd.NA, real string) to a display string.
    Guards against `float NaN or "—"` returning the NaN — a Python truthiness trap."""
    try:
        if v is None or pd.isna(v):
            return default
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return s if s and s.lower() not in ("nan", "none", "nat") else default


def _fmt_money(v, ccy="INR"):
    try:
        n = float(v or 0)
        if ccy == "INR":
            return f"₹{n:,.0f}"
        return f"{ccy} {n:,.0f}"
    except Exception:
        return f"{ccy} {v}"


# ═══════════════════════════════════════════════════════════════════════════
#  START / STOP CONTROLS
# ═══════════════════════════════════════════════════════════════════════════
def _run_send_worker(send_fn):
    """Runs in a background thread. Updates _send_state as it progresses."""
    def progress(sent, failed, total):
        with _send_lock:
            _send_state["sent"]   = sent
            _send_state["failed"] = failed
            _send_state["total"]  = total
    # Read the ignore_schedule flag set by whichever button launched us.
    with _send_lock:
        ignore_sched = _send_state.get("ignore_schedule", False)
    result = autosend_due_drafts(
        send_fn,
        stop_event=_send_state["stop_event"],
        progress_cb=progress,
        ignore_schedule=ignore_sched,
    )
    with _send_lock:
        _send_state["sent"]          = len(result["sent"])
        _send_state["failed"]        = len(result["failed"])
        _send_state["total"]         = result["total"]
        _send_state["stopped_early"] = result["stopped_early"]
        _send_state["finished_at"]   = datetime.now()


def _launch_send(send_fn, ignore_schedule=False):
    """Start the background send worker. Shared by both send buttons."""
    _reset_send_state()
    stop_ev = threading.Event()
    with _send_lock:
        _send_state["stop_event"] = stop_ev
        _send_state["started_at"] = datetime.now()
        _send_state["ignore_schedule"] = ignore_schedule
    t = threading.Thread(target=_run_send_worker, args=(send_fn,), daemon=True)
    with _send_lock:
        _send_state["thread"] = t
    t.start()


def _render_send_all_controls(send_fn):
    """Renders EITHER the Start button OR the Stop button + live progress,
    depending on whether a send job is currently running."""
    snap = _send_state_snapshot()

    if not snap["running"]:
        # Show final result if the previous run just finished
        if snap["finished_at"] is not None:
            if snap["stopped_early"]:
                st.warning(
                    f"⛔ Stopped by user · sent {snap['sent']}, "
                    f"failed {snap['failed']}, remaining "
                    f"{snap['total'] - snap['sent'] - snap['failed']}."
                )
            else:
                st.success(f"✅ Done · sent {snap['sent']}, "
                           f"failed {snap['failed']}.")

        if st.button("📤 Send all due", use_container_width=True,
                     type="primary",
                     help="Immediately dispatch every draft whose send date "
                          "has arrived. Runs in a background thread — you "
                          "can stop it at any time with the Stop button."):
            if send_fn is None:
                st.error("No sender configured.")
            else:
                _launch_send(send_fn, ignore_schedule=False)
                st.rerun()

        # v3.5.4 — Send ALL pending regardless of scheduled date.
        if st.button("🚀 Send all pending now (ignore dates)",
                     use_container_width=True, type="secondary",
                     help="Dispatch EVERY pending/approved reminder right now, "
                          "even if its scheduled send date is in the future. "
                          "Use with care — this emails the whole queue."):
            if send_fn is None:
                st.error("No sender configured.")
            else:
                _launch_send(send_fn, ignore_schedule=True)
                st.rerun()
    else:
        # RUNNING — show Stop button + live counter, and auto-refresh
        done = snap["sent"] + snap["failed"]
        total = max(snap["total"], done, 1)
        st.progress(done / total,
                    text=f"Sending… {done}/{total} · "
                         f"✅ {snap['sent']} · ❌ {snap['failed']}")

        stop_clicked = st.button(
            "⛔ Stop sending", use_container_width=True, type="secondary",
            disabled=snap["stop_requested"],
            help="Stops after the currently-in-flight send completes. "
                 "No more emails will be dispatched.",
        )
        if stop_clicked:
            with _send_lock:
                if _send_state["stop_event"] is not None:
                    _send_state["stop_event"].set()
            st.warning("⛔ Stop requested — no further emails will be sent. "
                       "Waiting for the in-flight one to finish…")
            st.rerun()

        # Poll every 1s while the thread runs so counters update live
        time.sleep(1)
        st.rerun()



def render_notification_center(current_user_email: str,
                               send_fn=None) -> None:
    """
    Renders the Notification Center — replaces the old Agent Pipeline Setup.
    Matches the v4 React layout the user showed in the screenshot.
    """
    ensure_schema()
    st.markdown(NC_CSS, unsafe_allow_html=True)

    # v3.5: silently sync with reality — any pending drafts on invoices
    # that were recently sent get auto-cancelled here.  Prevents the
    # "already sent 238 but still showing 238 pending" state.
    sync_result = sync_center_with_sent_state()
    if sync_result["ghost_drafts_cancelled"] > 0:
        st.info(
            f"🧹 Cleaned up {sync_result['ghost_drafts_cancelled']} pending "
            f"drafts on invoices where a reminder was already sent recently."
        )

    # ── Header row with actions ────────────────────────────────────────
    df = _load_center()
    pending_n = int((df["status"].isin(["pending", "pending"])).sum()) if not df.empty else 0

    hdr_col1, hdr_col2, hdr_col3 = st.columns([3, 1, 1])
    with hdr_col1:
        st.markdown(
            f"<div class='nc-header'>"
            f"<h2 style='margin:0'>🔔 Notification Center</h2>"
            f"<span class='nc-badge'>{pending_n} awaiting review</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
        st.caption(
            "Reminder drafts appear here **5 days before** their scheduled "
            "send date. Edit & save to put your wording on record — or do "
            "nothing and the system sends automatically on schedule. "
            "Cancel to stop a send entirely."
        )
    with hdr_col2:
        if st.button("🔄 Refresh lifecycle", use_container_width=True,
                     help="Recompute reminder dates and generate any new drafts due within 5 days."):
            n = ensure_reminder_dates()
            m = generate_upcoming_drafts()
            st.success(f"Scheduled {n} reminders · created {m} new draft(s)")
            st.rerun()
    with hdr_col3:
        _render_send_all_controls(send_fn)

    st.divider()

    if df.empty:
        st.info("Nothing scheduled in the next few days 🎉  "
                "Once you import invoices, drafts will appear here "
                f"{NOTIFY_LEAD_DAYS} days before their send date.")
        return

    # ── Filters ─────────────────────────────────────────────────────────
    f1, f2, f3 = st.columns([2, 2, 3])
    with f1:
        status_filter = st.multiselect(
            "Status", options=["pending", "approved"],
            default=["pending", "approved"],
            format_func=lambda s: "awaiting review" if s == "pending" else "reviewed",
        )
    with f2:
        window = st.selectbox(
            "Sends within",
            ["All upcoming", "Today", "Next 2 days", "Next 5 days"],
            index=0,
        )
    with f3:
        search = st.text_input("🔎 Search client or invoice", "")

    today = date.today()
    df["sched"] = pd.to_datetime(df["scheduled_send_date"], errors="coerce").dt.date
    # NaT slips through 'if d' truthiness — check pd.isna explicitly.
    df["days_to_send"] = df["sched"].apply(
        lambda d: 999 if (d is None or pd.isna(d)) else (d - today).days
    )

    view = df[df["status"].isin(status_filter or [])].copy()
    if window == "Today":         view = view[view["days_to_send"] <= 0]
    elif window == "Next 2 days": view = view[view["days_to_send"] <= 2]
    elif window == "Next 5 days": view = view[view["days_to_send"] <= 5]
    if search:
        s = search.lower()
        view = view[view["client_name"].str.lower().str.contains(s, na=False) |
                    view["invoice_number"].str.lower().str.contains(s, na=False)]

    view = view.sort_values("days_to_send").reset_index(drop=True)
    st.caption(f"Showing **{len(view)}** draft(s)")

    # ── Table header ────────────────────────────────────────────────────
    hcols = st.columns([1.1, 2.5, 1.8, 1.4, 2.5, 2.0, 2.6])
    for c, txt in zip(hcols, ["Sends in", "Client", "Invoice", "Amount",
                              "Template", "Status", "Actions"]):
        c.markdown(f"**{txt}**")
    st.divider()

    # ── Row rendering ───────────────────────────────────────────────────
    for _, row in view.iterrows():
        cols = st.columns([1.1, 2.5, 1.8, 1.4, 2.5, 2.0, 2.6])
        cols[0].markdown(_days_chip(int(row["days_to_send"])), unsafe_allow_html=True)
        cols[1].write(_s(row["client_name"]))
        cols[2].write(_s(row["invoice_number"]))
        cols[3].write(_fmt_money(
            row["total_amount"] if pd.notna(row["total_amount"]) else row["amount"],
            _s(row["currency"], "INR"),
        ))
        cols[4].write(_s(row["template_used"])[:32])
        cols[5].markdown(_status_chip(row["status"]), unsafe_allow_html=True)

        a1, a2, a3 = cols[6].columns(3)
        did = int(row["id"])
        with a1:
            if st.button("✏️", key=f"rev_{did}", help="Review & edit"):
                st.session_state[f"open_drawer_{did}"] = True
        with a2:
            if st.button("📤", key=f"snd_{did}", help="Send now"):
                if send_fn is None:
                    st.error("No sender configured — cannot send from the UI.")
                else:
                    ok = send_one_now(did, send_fn, current_user_email)
                    if ok:
                        st.toast(f"✅ Reminder sent to {row['to_email']}", icon="✅")
                    else:
                        st.error(
                            f"Send failed for draft #{did}. "
                            "Common causes: (a) Gmail credentials.json / token.json "
                            "missing from the backend folder, (b) token expired — "
                            "delete token.json and restart to re-auth, "
                            "(c) recipient email is empty."
                        )
                    st.rerun()
        with a3:
            if st.button("🚫", key=f"cxl_{did}", help="Cancel — will NOT be sent"):
                with get_db(DB_PATH) as conn:
                    conn.execute("UPDATE email_drafts SET status='rejected' WHERE id=?", (did,))
                    conn.commit()
                st.warning(f"Draft #{did} cancelled")
                st.rerun()

        # Review drawer
        if st.session_state.get(f"open_drawer_{did}"):
            with st.expander(f"✏️  Review draft · {row['client_name']} · "
                             f"{row['invoice_number']}", expanded=True):
                new_subj = st.text_input("Subject", _s(row["subject"], ""), key=f"sub_{did}")
                new_cc   = st.text_input("CC", _s(row["cc_email"], ""), key=f"cc_{did}",
                                         help="Comma-separated CC recipients")
                new_body = st.text_area("Body", _s(row["body"], ""), height=280, key=f"bod_{did}")
                bcol1, bcol2, bcol3 = st.columns(3)
                if bcol1.button("💾 Save (mark reviewed)", key=f"save_{did}",
                                type="primary", use_container_width=True):
                    with get_db(DB_PATH) as conn:
                        conn.execute(
                            "UPDATE email_drafts SET subject=?, body=?, cc_email=?, "
                            "status='approved', reviewed_by=?, "
                            "reviewed_at=datetime('now','localtime') WHERE id=?",
                            (new_subj, new_body, new_cc, current_user_email, did))
                        conn.commit()
                    st.session_state[f"open_drawer_{did}"] = False
                    st.success("Saved — will auto-send on schedule.")
                    st.rerun()
                if bcol2.button("❌ Close", key=f"cls_{did}", use_container_width=True):
                    st.session_state[f"open_drawer_{did}"] = False
                    st.rerun()
                bcol3.caption(
                    f"Scheduled: **{_s(row['scheduled_send_date'])}**  ·  "
                    f"To: {_s(row['to_email'])}  ·  Template: {_s(row['template_used'])}"
                )

    # ── History expander ────────────────────────────────────────────────
    st.divider()
    with st.expander("📜 Recently sent / cancelled (last 200)"):
        hist = _load_center(only_active=False)
        hist = hist[~hist["status"].isin(ACTIVE_STATUSES)]
        if hist.empty:
            st.caption("No history yet.")
        else:
            hist_view = hist[[
                "scheduled_send_date", "client_name", "invoice_number",
                "original_due_date", "latest_due_date",
                "expected_payment_date",
                "status", "template_used", "reviewed_by", "sent_at",
            ]].copy()
            hist_view = hist_view.rename(columns={
                "scheduled_send_date":    "Scheduled send",
                "client_name":            "Client",
                "invoice_number":         "Invoice",
                "original_due_date":      "Original Due Date",
                "latest_due_date":        "Latest Due Date",
                "expected_payment_date":  "Latest PTP Date",
                "status":                 "Status",
                "template_used":          "Template",
                "reviewed_by":            "Reviewed by",
                "sent_at":                "Sent at",
            })
            st.dataframe(hist_view, use_container_width=True, hide_index=True)


# helper to make "send now" advance just this one draft's scheduled date
def send_one_now(draft_id: int, send_fn, actor_email: str = "system") -> bool:
    """Immediately dispatch ONE draft. Reads it from the DB, calls send_fn,
    marks the outcome. Returns True on success."""
    with get_db(DB_PATH) as conn:
        d = conn.execute(
            "SELECT id, to_email, cc_email, subject, body FROM email_drafts "
            "WHERE id = ?", (draft_id,)
        ).fetchone()
        if not d or not d["to_email"]:
            return False
        try:
            ok = bool(send_fn(to=d["to_email"], subject=d["subject"],
                              body=d["body"], cc=d["cc_email"] or None))
        except Exception:
            ok = False
        if ok:
            conn.execute(
                "UPDATE email_drafts SET status='sent', "
                "sent_at=datetime('now','localtime'), reviewed_by=? WHERE id=?",
                (actor_email, draft_id),
            )
        else:
            conn.execute("UPDATE email_drafts SET status='failed' WHERE id=?",
                         (draft_id,))
        conn.commit()
    return ok
