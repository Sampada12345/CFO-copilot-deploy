"""
ptp_intelligence.py — v3.3
─────────────────────────────────────────────────────────────────────────────
Promise-to-Pay analysis + AI-driven client communication timeline.

WHAT THIS MODULE DOES
─────────────────────
1. Schema evolution (idempotent) that adds:
     • invoices.original_due_date  — captured on first ingest, NEVER changes
     • invoices.latest_due_date    — updated by PTP replies
     • invoices.extension_count    — # of PTP promises against this invoice
     • invoices.expected_payment_date — latest client-promised date
     • new table `client_replies`  — every scanned inbound reply
     • new table `ptp_events`      — every promise-to-pay commitment we've
                                     extracted, with full lineage
2. LLM extractor (Ollama first, deterministic-regex fallback) that turns
   natural-language replies into structured commitments:
     "I'll pay Friday"           → date, category='specific_day'
     "our cycle is on the 15th"  → date, category='cycle'
     "next week"                 → date range midpoint, category='vague'
     "payment initiated"         → no date, category='claim_initiated'
     "waiting for approval"      → no date, category='blocked_internal'
3. PTP rule engine — critical nuance from user:
     A commitment counts as a PTP ONLY if the promised date is AFTER the
     invoice's CURRENT due date (invoices.latest_due_date). If we send
     "due in 2 days" and client says "will pay in 2 days", that's just
     confirmation, not a promise-to-pay.
4. Gmail intake — hooks into gmail_client.fetch_invoice_emails and threads
   replies to invoices by invoice_number substring + client email match.
5. Analytics that Tab 1 and Tab 2 consume:
     ptp_summary()            portfolio-wide PTP KPIs
     invoice_ptp_status()     per-invoice status card fields
     client_communication_timeline()  chat-style event list for Tab 2

EVERYTHING IS ADDITIVE — no existing v3.2 functionality is removed.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
import streamlit as st

from core.database import DB_PATH, get_db

logger = logging.getLogger("cfo.ptp")

OLLAMA_HOST  = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")

# ═══════════════════════════════════════════════════════════════════════════
#  1. SCHEMA
# ═══════════════════════════════════════════════════════════════════════════
def ensure_schema() -> None:
    """Idempotent — safe to call on every app load. Adds v3.3+ columns/tables
    without touching existing data.

    Hrana/Turso-safe: the old version blindly ran every ALTER and swallowed the
    'duplicate column' error. On Turso that swallowed failure leaves the
    connection's stream in an aborted-transaction state, so the NEXT statement
    (e.g. the original_due_date back-fill) blows up with 'no such column' and
    takes the whole app down. Instead we read PRAGMA table_info first and only
    add columns that are genuinely missing, committing on success and rolling
    back on failure so a poisoned transaction never carries forward.
    """
    with get_db(DB_PATH) as conn:
        # Begin from a clean transaction state (the connection may arrive with
        # an open/aborted txn on some backends).
        try:
            conn.rollback()
        except Exception:
            pass

        def _columns(table: str) -> set[str]:
            try:
                return {r[1] for r in conn.execute(
                    f"PRAGMA table_info({table})").fetchall()}
            except Exception:
                return set()

        wanted = {
            "invoices": [
                ("original_due_date",     "TEXT"),
                ("latest_due_date",       "TEXT"),
                ("extension_count",       "INTEGER DEFAULT 0"),
                ("expected_payment_date", "TEXT"),
            ],
            "email_drafts": [
                ("template_used",       "TEXT"),
                ("scheduled_send_date", "TEXT"),
                ("reviewed_by",         "TEXT"),
                ("reviewed_at",         "TEXT"),
            ],
            # client_replies is (re)created just below; on a fresh DB it won't
            # exist here yet (skipped), on an older DB this adds the column.
            "client_replies": [
                ("direction", "TEXT DEFAULT 'in'"),
            ],
        }
        for table, cols in wanted.items():
            existing = _columns(table)
            if not existing:
                continue  # table not created yet — the CREATEs below handle it
            for col, ddl in cols:
                if col in existing:
                    continue
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS client_replies (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_message_id  TEXT UNIQUE,
                client_id         INTEGER,
                invoice_id        INTEGER,          -- best-effort match; may be NULL
                thread_id         TEXT,
                subject           TEXT,
                body              TEXT,
                received_at       TEXT,
                ai_category       TEXT,             -- specific_day | cycle | vague |
                                                    -- claim_initiated | blocked_internal |
                                                    -- disputed | no_commitment
                ai_summary        TEXT,
                ai_promised_date  TEXT,             -- YYYY-MM-DD or NULL
                ai_confidence     REAL,
                is_ptp            INTEGER DEFAULT 0,-- set to 1 by rule engine
                direction         TEXT DEFAULT 'in',-- 'in' = reply | 'out' = sent reminder
                created_at        TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (client_id)  REFERENCES clients(id),
                FOREIGN KEY (invoice_id) REFERENCES invoices(id)
            );
            CREATE INDEX IF NOT EXISTS idx_replies_client
                ON client_replies (client_id, received_at);
            CREATE INDEX IF NOT EXISTS idx_replies_invoice
                ON client_replies (invoice_id, received_at);

            CREATE TABLE IF NOT EXISTS ptp_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id      INTEGER NOT NULL,
                client_id       INTEGER NOT NULL,
                reply_id        INTEGER,            -- source client_replies row
                promised_date   TEXT NOT NULL,
                previous_due    TEXT NOT NULL,      -- latest_due_date before this event
                days_extended   INTEGER NOT NULL,
                created_at      TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (invoice_id) REFERENCES invoices(id),
                FOREIGN KEY (client_id)  REFERENCES clients(id),
                FOREIGN KEY (reply_id)   REFERENCES client_replies(id)
            );
            CREATE INDEX IF NOT EXISTS idx_ptp_invoice ON ptp_events (invoice_id);
        """)
        conn.commit()

        # Back-fill original_due_date / latest_due_date for rows that predate
        # v3.3. Guarded individually so a problem here can never take the app
        # down (the columns now exist both in the base schema and via the
        # ALTERs above, so this should always succeed).
        for stmt in (
            "UPDATE invoices SET original_due_date = due_date "
            "WHERE original_due_date IS NULL AND due_date IS NOT NULL",
            "UPDATE invoices SET latest_due_date = due_date "
            "WHERE latest_due_date IS NULL AND due_date IS NOT NULL",
        ):
            try:
                conn.execute(stmt)
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass


# ═══════════════════════════════════════════════════════════════════════════
#  2. LLM + FALLBACK EXTRACTOR
# ═══════════════════════════════════════════════════════════════════════════
LLM_PROMPT = """You are analysing a client email reply about a payment.
Extract structured info as strict JSON only. No prose, no markdown.

Fields:
  "category": one of ["specific_day","cycle","vague","claim_initiated",
                       "blocked_internal","disputed","no_commitment"]
  "promised_date": "YYYY-MM-DD" or null
  "summary": one factual sentence describing what the client said
  "confidence": 0.0-1.0

Rules:
  • Resolve relative dates using TODAY = {today}.
  • "in N days" / "N days" → today + N.
  • "next week" → today + 7 (Wednesday of that week).
  • "on the Nth" → next occurrence of that day in this or next month.
  • "Friday" / weekday → next occurrence of that weekday.
  • "payment cycle is on the Nth" → category=cycle, date = next Nth.
  • "initiated / processed / released" without a date → claim_initiated, null date.
  • "waiting for approval / sign-off" → blocked_internal, null date.
  • "dispute / incorrect / wrong amount" → disputed, null date.
  • Nothing extractable → no_commitment, null date, low confidence.

EMAIL FROM: {sender}
SUBJECT:    {subject}
BODY:
{body}
"""


def _iso(d: date | datetime | None) -> str | None:
    if d is None:
        return None
    return d.date().isoformat() if isinstance(d, datetime) else d.isoformat()


def _try_ollama(prompt: str, timeout: int = 45) -> dict | None:
    """Call the same Ollama endpoint pipeline.py already uses. Returns
    None on any failure so the caller falls back to regex."""
    try:
        import requests
        r = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            timeout=timeout,
            json={"model": OLLAMA_MODEL, "prompt": prompt,
                  "stream": False, "format": "json"},
        )
        r.raise_for_status()
        data = json.loads(r.json().get("response", "{}"))
        if data.get("category"):
            return data
    except Exception:
        return None
    return None


# ── deterministic fallback ─────────────────────────────────────────────────
WEEKDAYS = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,
             "friday":4,"saturday":5,"sunday":6,"mon":0,"tue":1,"wed":2,
             "thu":3,"fri":4,"sat":5,"sun":6}

# Word-form numerals — "in two days" etc.
NUMBER_WORDS = {"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,
                 "seven":7,"eight":8,"nine":9,"ten":10,"eleven":11,
                 "twelve":12,"a couple":2,"couple":2,"a few":3,"few":3}

RE_IN_DAYS   = re.compile(r"\b(?:in|within|after)\s+(\d{1,2})\s*days?\b", re.I)
RE_IN_WORDS  = re.compile(
    r"\b(?:in|within|after)\s+(one|two|three|four|five|six|seven|eight|"
    r"nine|ten|eleven|twelve|a\s+couple|couple|a\s+few|few)\s+days?\b", re.I)
RE_DAYS_ONLY = re.compile(r"\b(\d{1,2})\s+days?\b", re.I)
RE_ORDINAL   = re.compile(r"\bon\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
RE_CYCLE     = re.compile(r"\bcycle.{0,20}?(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
RE_ISO_DATE  = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

# Weekday match: must be preceded by a "commitment" verb, not just any
# occurrence.  Prevents "I emailed you Friday" from being treated as a promise.
RE_WEEKDAY_PROMISE = re.compile(
    r"\b(?:pay|paying|will|by|processed?\s+on|process\s+on|clear|"
    r"remit|send|release)\b[^.]{0,40}?\b(monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\b", re.I)


def _next_ordinal(today: date, day: int) -> date | None:
    if not 1 <= day <= 31: return None
    y, m = today.year, today.month
    for _ in range(3):
        try:
            candidate = date(y, m, day)
        except ValueError:
            m += 1
            if m > 12: m, y = 1, y + 1
            continue
        if candidate > today:
            return candidate
        m += 1
        if m > 12: m, y = 1, y + 1
    return None


def _next_weekday(today: date, wd: int) -> date:
    delta = (wd - today.weekday()) % 7
    return today + timedelta(days=delta or 7)


def _regex_extract(body: str, today: date) -> dict:
    """Best-effort no-LLM parser. Returns the same schema as the LLM."""
    text = (body or "").lower()

    if any(k in text for k in ["dispute","incorrect","wrong amount",
                                "not our","credit note","disagree"]):
        return {"category":"disputed","promised_date":None,
                "summary":"Client disputes the invoice.","confidence":0.5}

    if any(k in text for k in ["waiting for approval","sign-off","sign off",
                                "management approval","approval pending",
                                "on hold"]):
        return {"category":"blocked_internal","promised_date":None,
                "summary":"Client blocked on internal approval.",
                "confidence":0.5}

    # ── SPECIFIC DATES first (before catch-all claim keywords).  A phrase
    # like "will pay processed on Friday" should return Friday, not just
    # "claim_initiated". ──

    # ISO date wins if present
    m = RE_ISO_DATE.search(text)
    if m:
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            return {"category":"specific_day","promised_date":_iso(d),
                    "summary":f"Promised specific date {d.isoformat()}.",
                    "confidence":0.7}
        except ValueError: pass

    # "cycle on the Nth"
    m = RE_CYCLE.search(text)
    if m:
        d = _next_ordinal(today, int(m.group(1)))
        if d:
            return {"category":"cycle","promised_date":_iso(d),
                    "summary":f"Client payment cycle on the {m.group(1)}.",
                    "confidence":0.55}

    # "on the 15th"
    m = RE_ORDINAL.search(text)
    if m:
        d = _next_ordinal(today, int(m.group(1)))
        if d:
            return {"category":"specific_day","promised_date":_iso(d),
                    "summary":f"Promised payment on the {m.group(1)}.",
                    "confidence":0.55}

    # "in N days" (digit)
    m = RE_IN_DAYS.search(text)
    if m:
        d = today + timedelta(days=int(m.group(1)))
        return {"category":"specific_day","promised_date":_iso(d),
                "summary":f"Will pay in {m.group(1)} days.",
                "confidence":0.6}

    # "in two days", "in a few days" — word-form numerals
    m = RE_IN_WORDS.search(text)
    if m:
        n = NUMBER_WORDS.get(m.group(1).lower().strip(), None)
        if n is not None:
            d = today + timedelta(days=n)
            return {"category":"specific_day","promised_date":_iso(d),
                    "summary":f"Will pay in {m.group(1)} days ({n}).",
                    "confidence":0.55}

    # Weekday only if preceded by a promise verb
    m = RE_WEEKDAY_PROMISE.search(text)
    if m:
        name = m.group(1).lower()
        d = _next_weekday(today, WEEKDAYS[name])
        return {"category":"specific_day","promised_date":_iso(d),
                "summary":f"Promised payment by {name.title()}.",
                "confidence":0.5}

    if "next week"  in text:
        d = today + timedelta(days=7)
        return {"category":"vague","promised_date":_iso(d),
                "summary":"Vague commitment: next week.","confidence":0.35}
    if "next month" in text:
        d = today + timedelta(days=30)
        return {"category":"vague","promised_date":_iso(d),
                "summary":"Vague commitment: next month.","confidence":0.3}

    # "N days" without "in" — weaker signal
    m = RE_DAYS_ONLY.search(text)
    if m:
        d = today + timedelta(days=int(m.group(1)))
        return {"category":"specific_day","promised_date":_iso(d),
                "summary":f"Approximately {m.group(1)} days.",
                "confidence":0.4}

    # Claim keywords — LAST, so specific dates aren't overridden
    if any(k in text for k in ["initiated","processing","processed","released",
                                "transferred","utr","remitted","paid on",
                                "payment made"]):
        return {"category":"claim_initiated","promised_date":None,
                "summary":"Client claims payment has been initiated or processed.",
                "confidence":0.5}

    return {"category":"no_commitment","promised_date":None,
            "summary":"No payment commitment extractable.","confidence":0.2}


def extract_reply(sender: str, subject: str, body: str,
                  today: date | None = None) -> dict:
    """Public entry: LLM first, deterministic fallback if Ollama is down."""
    today = today or date.today()
    llm = _try_ollama(LLM_PROMPT.format(
        today=today.isoformat(), sender=sender or "",
        subject=subject or "", body=(body or "")[:4000],
    ))
    if llm and llm.get("category"):
        return llm
    return _regex_extract(body, today)


# ═══════════════════════════════════════════════════════════════════════════
#  3. PTP RULE ENGINE
# ═══════════════════════════════════════════════════════════════════════════
def _to_date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def apply_reply_to_invoice(conn, reply_id: int) -> dict:
    """Apply the AI-extracted commitment to the linked invoice, following
    the strict PTP rule: a commitment only counts as a PTP if the promised
    date is AFTER the invoice's CURRENT due date."""
    reply = conn.execute("SELECT * FROM client_replies WHERE id=?",
                         (reply_id,)).fetchone()
    if not reply or not reply["invoice_id"] or not reply["ai_promised_date"]:
        return {"applied": False, "reason": "no promise or no invoice link"}

    inv = conn.execute(
        "SELECT id, original_due_date, latest_due_date, due_date, "
        "extension_count FROM invoices WHERE id=?", (reply["invoice_id"],)
    ).fetchone()
    if not inv:
        return {"applied": False, "reason": "invoice missing"}

    promised = _to_date(reply["ai_promised_date"])
    if not promised:
        return {"applied": False, "reason": "unparseable promised date"}

    current_due = _to_date(inv["latest_due_date"]) or _to_date(inv["due_date"])
    if not current_due:
        return {"applied": False, "reason": "invoice has no due date"}

    # ── THE PTP RULE per user requirement ──────────────────────────────
    if promised <= current_due:
        # Just a confirmation of what we already asked. Not a PTP.
        conn.execute("UPDATE client_replies SET is_ptp = 0 WHERE id = ?",
                     (reply_id,))
        conn.commit()
        return {"applied": False, "reason":
                f"promised date {promised} is not later than current due "
                f"{current_due} — treated as confirmation, not PTP"}

    original_due = _to_date(inv["original_due_date"]) or current_due
    days_extended = (promised - current_due).days
    # Days delayed vs ORIGINAL is what shows on the dashboard:
    days_delayed_vs_original = (promised - original_due).days

    conn.execute("""
        UPDATE invoices
           SET latest_due_date        = ?,
               expected_payment_date  = ?,
               extension_count        = COALESCE(extension_count, 0) + 1,
               updated_at             = datetime('now','localtime')
         WHERE id = ?
    """, (promised.isoformat(), promised.isoformat(), inv["id"]))

    conn.execute("""
        INSERT INTO ptp_events (invoice_id, client_id, reply_id,
                                promised_date, previous_due, days_extended)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (inv["id"], reply["client_id"], reply_id,
          promised.isoformat(), current_due.isoformat(), days_extended))

    conn.execute("UPDATE client_replies SET is_ptp = 1 WHERE id = ?",
                 (reply_id,))
    conn.commit()
    return {
        "applied": True,
        "invoice_id": inv["id"],
        "promised": promised.isoformat(),
        "previous_due": current_due.isoformat(),
        "days_extended": days_extended,
        "days_delayed_vs_original": days_delayed_vs_original,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  4. GMAIL INTAKE
# ═══════════════════════════════════════════════════════════════════════════
INVOICE_NUM_RE = re.compile(r"\b(ITPL/\d{2}-\d{2}/\d+|INV[-/]?\d+)\b", re.I)


def _match_reply_to_invoice(conn, sender: str, subject: str,
                             body: str) -> tuple[int | None, int | None]:
    """Best-effort: find (client_id, invoice_id) that the reply refers to."""
    client = conn.execute(
        "SELECT id FROM clients WHERE lower(email) = lower(?)", (sender,)
    ).fetchone()
    client_id = client["id"] if client else None

    invoice_id = None
    m = INVOICE_NUM_RE.search(subject or "") or INVOICE_NUM_RE.search(body or "")
    if m:
        num = m.group(1)
        row = conn.execute(
            "SELECT id, client_id FROM invoices WHERE invoice_number=? "
            "ORDER BY created_at DESC LIMIT 1", (num,)
        ).fetchone()
        if row:
            invoice_id = row["id"]
            if client_id is None:
                client_id = row["client_id"]

    # Fall back: if we know the client, pick their newest still-open invoice.
    if invoice_id is None and client_id is not None:
        row = conn.execute("""
            SELECT id FROM invoices
             WHERE client_id = ?
               AND (status IS NULL OR status NOT IN ('paid','void'))
             ORDER BY due_date DESC LIMIT 1
        """, (client_id,)).fetchone()
        if row:
            invoice_id = row["id"]

    return client_id, invoice_id


def ingest_reply(conn, email: dict, today: date | None = None,
                 direction: str = "in", counterparty_email: str | None = None) -> dict:
    """Store one scanned email. Dedupes on gmail_message_id so a rerun is
    idempotent.

    direction='in'  → an INBOUND client reply: match on the sender, run the
                      AI extraction, and apply any PTP.
    direction='out' → an OUTBOUND reminder we sent (from the Gmail SENT box):
                      the client is the RECIPIENT, not the sender, so match on
                      `counterparty_email`. We do NOT run PTP extraction on our
                      own outbound mail — it's stored purely so the Tab-2
                      timeline can show manually-sent reminders.
    """
    ensure_schema()
    gid = email.get("id") or email.get("gmail_message_id")
    if gid:
        dup = conn.execute("SELECT id FROM client_replies WHERE gmail_message_id=?",
                           (gid,)).fetchone()
        if dup:
            return {"stored": False, "reason": "already ingested",
                    "reply_id": dup["id"], "direction": direction}

    subject = email.get("subject") or ""
    body    = email.get("body") or ""

    # ── OUTBOUND (sent box) ──────────────────────────────────────────────
    if direction == "out":
        # The counterparty (client) is who we sent TO. Resolve them, then link
        # to their most relevant invoice via the shared matcher.
        cp = (counterparty_email or "").strip().lower()
        client_id, invoice_id = _match_reply_to_invoice(conn, cp, subject, body)
        cur = conn.execute("""
            INSERT INTO client_replies
              (gmail_message_id, client_id, invoice_id, thread_id,
               subject, body, received_at,
               ai_category, ai_summary, ai_promised_date, ai_confidence,
               is_ptp, direction)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (gid, client_id, invoice_id, email.get("thread_id"),
              subject[:400], body[:4000], email.get("date", ""),
              None, None, None, 0.0, 0, "out"))
        reply_id = cur.lastrowid
        conn.commit()
        return {"stored": True, "reply_id": reply_id, "direction": "out",
                "invoice_id": invoice_id, "client_id": client_id}

    # ── INBOUND (client reply) ───────────────────────────────────────────
    sender  = (email.get("from") or "").strip().lower()

    client_id, invoice_id = _match_reply_to_invoice(conn, sender, subject, body)

    ai = extract_reply(sender, subject, body, today=today)

    cur = conn.execute("""
        INSERT INTO client_replies
          (gmail_message_id, client_id, invoice_id, thread_id,
           subject, body, received_at,
           ai_category, ai_summary, ai_promised_date, ai_confidence, direction)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (gid, client_id, invoice_id, email.get("thread_id"),
          subject[:400], body[:4000], email.get("date", ""),
          ai.get("category"), ai.get("summary"),
          ai.get("promised_date"), float(ai.get("confidence") or 0), "in"))
    reply_id = cur.lastrowid
    conn.commit()

    result = {"stored": True, "reply_id": reply_id, "direction": "in",
              "category": ai.get("category"), "promised": ai.get("promised_date"),
              "invoice_id": invoice_id, "client_id": client_id}

    if invoice_id and ai.get("promised_date"):
        result["ptp"] = apply_reply_to_invoice(conn, reply_id)
    return result


def _all_client_emails() -> set[str]:
    """Every client email we know about (lowercased), straight from the
    `clients` table.

    This REPLACES the old 'only clients we already app-emailed' universe,
    which came from email_drafts.status='sent'. That list was empty on any
    fresh DB (i.e. every ephemeral-cloud restart) and whenever reminders
    were sent manually from Gmail — so the whole reply scan silently bailed
    out. Matching against all known clients is what lets old/again-scanned
    replies actually land."""
    with get_db(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT DISTINCT lower(email) AS email
              FROM clients
             WHERE email IS NOT NULL AND email != ''
        """).fetchall()
    return {r["email"] for r in rows if r["email"]}


_EMAIL_TOKEN_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _email_in_header(header: str, known: set[str]) -> str | None:
    """Return the first known client email that appears as a full address in a
    raw header (handles 'Name <a@b.com>' and comma-separated lists), else None.
    Used to resolve the counterparty on SENT-box mail, where the client is the
    recipient (To), not the sender. Matches whole email tokens — not substrings
    — so e.g. 'on@corp.com' won't falsely match inside 'jon@corp.com'."""
    if not header:
        return None
    for tok in _EMAIL_TOKEN_RE.findall(header.lower()):
        if tok in known:
            return tok
    return None


def _list_message_ids(service, query: str, cap: int = 500,
                      page_size: int = 100) -> list[str]:
    """Paginate Gmail's messages.list for `query`, returning up to `cap` ids.

    The old code fetched only the first page (max 100), so anything past the
    first page in the window was invisible — part of why 'older replies'
    never showed up. We page through until we hit `cap` or run out."""
    ids: list[str] = []
    page_token = None
    while len(ids) < cap:
        try:
            resp = service.users().messages().list(
                userId="me", q=query,
                maxResults=min(page_size, cap - len(ids)),
                pageToken=page_token,
            ).execute()
        except Exception as e:
            logger.warning("Gmail list failed for %r: %s", query, e)
            break
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _fetch_mailbox(service, query: str, cap: int = 500) -> list[dict]:
    """Fetch + parse every message matching `query` (up to `cap`). Deduped by
    message id. Returns parsed dicts from the shared _parse_message()."""
    from services.gmail_client import _parse_message  # reuse existing parser
    out: list[dict] = []
    seen: set[str] = set()
    for mid in _list_message_ids(service, query, cap=cap):
        if mid in seen:
            continue
        seen.add(mid)
        try:
            raw = service.users().messages().get(
                userId="me", id=mid, format="full",
            ).execute()
            parsed = _parse_message(raw)
            if parsed:
                out.append(parsed)
        except Exception:
            continue
    return out


def _record_scan(result: dict) -> None:
    """Persist a scan result for the Tab-3 job-history panel."""
    with get_db(DB_PATH) as conn:
        # scan_history table — created lazily so old DBs don't break
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at       TEXT DEFAULT (datetime('now','localtime')),
                fetched      INTEGER,
                processed    INTEGER,
                ptps         INTEGER,
                targeted     INTEGER,
                days_back    INTEGER,
                error        TEXT
            )
        """)
        conn.execute("""
            INSERT INTO scan_history (fetched, processed, ptps, targeted,
                                       days_back, error)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            result.get("fetched", 0),
            result.get("processed", 0),
            result.get("ptps", 0),
            result.get("targeted_clients", 0),
            int(os.getenv("SCAN_DAYS_BACK", "30")),
            result.get("error"),
        ))
        # Keep only the last 200 rows so this table doesn't bloat
        conn.execute("""
            DELETE FROM scan_history
             WHERE id NOT IN (SELECT id FROM scan_history
                              ORDER BY ran_at DESC LIMIT 200)
        """)
        conn.commit()


def _is_invoice_related(subject: str, body: str) -> bool:
    """True if the message references an invoice number — lets us catch a
    genuine reply even when it arrives from an address we don't have on file."""
    text = f"{subject or ''}\n{body or ''}"
    return bool(INVOICE_NUM_RE.search(text))


def poll_gmail_replies(max_results: int = 500, days_back: int = None) -> dict:
    """Scan real Gmail for client replies (inbox) AND reminders we sent
    (sent box), within the last `days_back` days.

    WHAT CHANGED (v3.6)
      • No longer gated on 'clients we already app-emailed'. The candidate
        universe is EVERY known client (clients table), so a fresh/cloud DB
        or manually-sent reminders no longer make the scan a no-op.
      • Scans in:inbox AND in:sent, paginated through the whole window, so
        older replies are pulled in — not just the first page.
      • Relevance filter: a message is kept only if its counterparty is a
        known client, OR it references an invoice number. Keeps newsletters
        and unrelated mail out of client_replies / PTP analytics.
      • Inbox → stored as inbound (direction='in') + AI extraction + PTP.
        Sent  → stored as outbound (direction='out'), no PTP, for the
                Tab-2 timeline.

    Deduped by gmail_message_id, so calling it repeatedly is safe.
    Every run is persisted to `scan_history` for Tab 3.

    Returns:
        {fetched, processed, inbound, outbound, ptps, targeted_clients,
         days_back, error?}
    """
    if days_back is None:
        days_back = int(os.getenv("SCAN_DAYS_BACK", "30"))

    ensure_schema()
    known = _all_client_emails()

    try:
        from services.gmail_client import get_gmail_service
        svc = get_gmail_service()
        inbox_msgs = _fetch_mailbox(svc, f"in:inbox newer_than:{days_back}d",
                                    cap=max_results)
        sent_msgs  = _fetch_mailbox(svc, f"in:sent newer_than:{days_back}d",
                                    cap=max_results)
    except Exception as e:
        result = {"fetched": 0, "processed": 0, "inbound": 0, "outbound": 0,
                  "ptps": 0, "targeted_clients": len(known),
                  "days_back": days_back, "error": str(e)}
        _record_scan(result)
        return result

    fetched = len(inbox_msgs) + len(sent_msgs)
    inbound = outbound = ptps = 0

    with get_db(DB_PATH) as conn:
        # ── INBOX: potential client replies ──────────────────────────────
        for em in inbox_msgs:
            sender  = (em.get("from") or "").strip().lower()
            subject = em.get("subject") or ""
            body    = em.get("body") or ""
            relevant = (sender in known) or _is_invoice_related(subject, body)
            if not relevant:
                continue
            res = ingest_reply(conn, em, direction="in")
            if res.get("stored"):
                inbound += 1
                if res.get("ptp", {}).get("applied"):
                    ptps += 1

        # ── SENT: reminders we dispatched (incl. manual ones) ────────────
        for em in sent_msgs:
            to_hdr  = em.get("to") or ""
            subject = em.get("subject") or ""
            body    = em.get("body") or ""
            cp = _email_in_header(to_hdr, known)
            relevant = bool(cp) or _is_invoice_related(subject, body)
            if not relevant:
                continue
            res = ingest_reply(conn, em, direction="out", counterparty_email=cp)
            if res.get("stored"):
                outbound += 1

    processed = inbound + outbound
    result = {"fetched": fetched, "processed": processed,
              "inbound": inbound, "outbound": outbound, "ptps": ptps,
              "targeted_clients": len(known), "days_back": days_back}
    _record_scan(result)
    return result


def recent_scan_history(n: int = 10) -> list[dict]:
    """For Tab 3's job-history panel."""
    with get_db(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at TEXT DEFAULT (datetime('now','localtime')),
                fetched INTEGER, processed INTEGER, ptps INTEGER,
                targeted INTEGER, days_back INTEGER, error TEXT
            )
        """)
        rows = conn.execute("""
            SELECT ran_at, fetched, processed, ptps, targeted, days_back, error
              FROM scan_history ORDER BY ran_at DESC LIMIT ?
        """, (n,)).fetchall()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
#  5. ANALYTICS
# ═══════════════════════════════════════════════════════════════════════════
def invoice_ptp_status(invoice_id: int) -> dict:
    """The card fields Tab 2 shows for one invoice."""
    ensure_schema()
    with get_db(DB_PATH) as conn:
        inv = conn.execute("""
            SELECT id, invoice_number, due_date, original_due_date,
                   latest_due_date, expected_payment_date, extension_count,
                   status, total_amount, amount, currency
              FROM invoices WHERE id = ?
        """, (invoice_id,)).fetchone()
        if not inv:
            return {}
        events = conn.execute("""
            SELECT promised_date, previous_due, days_extended, created_at
              FROM ptp_events WHERE invoice_id = ?
             ORDER BY created_at
        """, (invoice_id,)).fetchall()

    original = _to_date(inv["original_due_date"]) or _to_date(inv["due_date"])
    latest   = _to_date(inv["latest_due_date"])   or original
    ptp_date = _to_date(inv["expected_payment_date"]) or latest
    days_delayed = (latest - original).days if (original and latest) else 0

    return {
        "invoice_number":      inv["invoice_number"],
        "original_due":        original.isoformat() if original else None,
        "latest_due":          latest.isoformat()   if latest   else None,
        "ptp_date":            ptp_date.isoformat() if ptp_date else None,
        "days_delayed":        max(days_delayed, 0),
        "extension_count":     inv["extension_count"] or 0,
        "status":              inv["status"],
        "amount":              inv["total_amount"] or inv["amount"] or 0,
        "currency":            inv["currency"] or "INR",
        "ptp_history":         [dict(r) for r in events],
    }


@st.cache_data(ttl=300)
def ptp_summary() -> dict:
    """Portfolio-level PTP KPIs — Tab 1 shows these."""
    ensure_schema()
    with get_db(DB_PATH) as conn:
        total  = conn.execute("SELECT COUNT(*) FROM ptp_events").fetchone()[0]
        by_ext = conn.execute("""
            SELECT extension_count, COUNT(*) AS n
              FROM invoices
             WHERE extension_count > 0
             GROUP BY extension_count
             ORDER BY extension_count
        """).fetchall()
        avg_ext = conn.execute("""
            SELECT AVG(days_extended) FROM ptp_events
        """).fetchone()[0]
        top_ext = conn.execute("""
            SELECT c.name AS client, COUNT(*) AS extension_count,
                   SUM(p.days_extended) AS total_days_extended,
                   AVG(p.days_extended) AS avg_days
              FROM ptp_events p
              JOIN clients c ON c.id = p.client_id
             GROUP BY p.client_id, c.name
             ORDER BY total_days_extended DESC
             LIMIT 10
        """).fetchall()
        repeat_offenders = conn.execute("""
            SELECT COUNT(DISTINCT client_id) FROM (
                SELECT client_id, COUNT(*) AS n
                  FROM ptp_events GROUP BY client_id HAVING COUNT(*) >= 3
            ) sub
        """).fetchone()[0]

    return {
        "total_ptps":            total,
        "avg_days_extended":     float(avg_ext) if avg_ext else 0.0,
        "extension_histogram":   [dict(r) for r in by_ext],
        "top_extenders":         [dict(r) for r in top_ext],
        "repeat_offenders":      int(repeat_offenders or 0),
    }


def _as_date(value) -> date | None:
    """Lenient date parser for BOTH SQLite datetimes ('2026-08-05 10:30:00')
    and RFC-2822 Gmail 'Date' headers ('Mon, 5 Aug 2026 10:30:00 +0530')."""
    if not value:
        return None
    s = str(value).strip()
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        return dt.date() if dt else None
    except Exception:
        return None


@st.cache_data(ttl=300)
def client_communication_timeline(client_id: int) -> list[dict]:
    """Chat-style event list for a client: outbound drafts + inbound replies,
    each with AI summary.  Sorted oldest → newest so Tab 2 can render it
    top-down.  Every event includes the linked invoice's original_due_date
    and latest_due_date + expected_payment_date so the UI can show them
    alongside the AI extraction."""
    ensure_schema()
    with get_db(DB_PATH) as conn:
        # Build a per-invoice dict so we join on the Python side (cheaper
        # than a JOIN for these small result sets and lets us keep both
        # queries simple).
        inv_rows = conn.execute("""
            SELECT id, invoice_number, original_due_date, latest_due_date,
                   expected_payment_date
              FROM invoices
             WHERE client_id = ?
        """, (client_id,)).fetchall()
        inv_by_id = {r["id"]: dict(r) for r in inv_rows}

        drafts = conn.execute("""
            SELECT id, invoice_id, subject, status, sent_at, created_at,
                   template_used
              FROM email_drafts
             WHERE client_id = ?
             ORDER BY COALESCE(sent_at, created_at)
        """, (client_id,)).fetchall()
        replies = conn.execute("""
            SELECT id, invoice_id, subject, body, received_at,
                   ai_category, ai_summary, ai_promised_date,
                   ai_confidence, is_ptp, COALESCE(direction,'in') AS direction
              FROM client_replies
             WHERE client_id = ?
             ORDER BY received_at
        """, (client_id,)).fetchall()

    def _inv_dates(inv_id):
        inv = inv_by_id.get(inv_id, {})
        return {
            "invoice_number":        inv.get("invoice_number"),
            "original_due_date":     inv.get("original_due_date"),
            "latest_due_date":       inv.get("latest_due_date"),
            "expected_payment_date": inv.get("expected_payment_date"),
        }

    # App-sent reminder dates (for dedup against sent-box scans). A reminder
    # sent THROUGH the app lives in email_drafts AND turns up in the Gmail
    # sent box, so without this we'd show it twice.
    app_sent_dates = [
        _as_date(d["sent_at"] or d["created_at"])
        for d in drafts if d["status"] == "sent"
    ]
    app_sent_dates = [d for d in app_sent_dates if d]

    def _already_shown_as_draft(when) -> bool:
        wd = _as_date(when)
        if wd is None:
            return False
        return any(abs((wd - ad).days) <= 2 for ad in app_sent_dates)

    events = []
    for d in drafts:
        events.append({
            "kind":      "outbound",
            "at":        d["sent_at"] or d["created_at"],
            "status":    d["status"],
            "subject":   d["subject"],
            "template":  d["template_used"],
            "invoice_id": d["invoice_id"],
            **_inv_dates(d["invoice_id"]),
        })
    for r in replies:
        if r["direction"] == "out":
            # A reminder we sent, discovered in the Gmail sent box. Skip it if
            # it's the same message an app-sent draft already represents.
            if _already_shown_as_draft(r["received_at"]):
                continue
            events.append({
                "kind":      "outbound",
                "at":        r["received_at"],
                "status":    "sent",
                "subject":   r["subject"],
                "template":  None,
                "via":       "gmail",          # manually-sent, not via the app
                "invoice_id": r["invoice_id"],
                **_inv_dates(r["invoice_id"]),
            })
            continue
        events.append({
            "kind":       "inbound",
            "at":         r["received_at"],
            "subject":    r["subject"],
            "body":       r["body"],
            "ai_category": r["ai_category"],
            "ai_summary":  r["ai_summary"],
            "ai_promised": r["ai_promised_date"],
            "confidence":  r["ai_confidence"],
            "is_ptp":     bool(r["is_ptp"]),
            "invoice_id": r["invoice_id"],
            **_inv_dates(r["invoice_id"]),
        })
    events.sort(key=lambda e: e["at"] or "")
    return events


def client_ptp_kpis(client_id: int) -> dict:
    ensure_schema()
    with get_db(DB_PATH) as conn:
        n_reminders = conn.execute(
            "SELECT COUNT(*) FROM email_drafts WHERE client_id=? AND status='sent'",
            (client_id,)).fetchone()[0]
        n_replies = conn.execute(
            "SELECT COUNT(*) FROM client_replies "
            "WHERE client_id=? AND COALESCE(direction,'in') <> 'out'",
            (client_id,)).fetchone()[0]
        n_ptps = conn.execute(
            "SELECT COUNT(*) FROM ptp_events WHERE client_id=?",
            (client_id,)).fetchone()[0]
        avg_ext = conn.execute(
            "SELECT AVG(days_extended) FROM ptp_events WHERE client_id=?",
            (client_id,)).fetchone()[0]
        last = conn.execute("""
            SELECT ai_summary, ai_promised_date, received_at, is_ptp
              FROM client_replies
             WHERE client_id=? AND COALESCE(direction,'in') <> 'out'
             ORDER BY received_at DESC LIMIT 1
        """, (client_id,)).fetchone()
    return {
        "reminders_sent":       n_reminders,
        "replies_received":     n_replies,
        "extension_requests":   n_ptps,
        "avg_days_extended":    float(avg_ext) if avg_ext else 0.0,
        "latest_commitment":    (dict(last) if last else None),
    }
