"""
FILE: core/database.py
PURPOSE: Everything related to the operational database.
         Creates tables, stores data, fetches data.
         Think of this as the "memory" of the entire system.

STORAGE BACKEND (v3.6 — Turso migration)
-----------------------------------------
This file used to hardcode Python's stdlib ``sqlite3``. It now speaks to
one of two backends, chosen automatcally at runtime — WITHOUT changing a
single call site elsewhere in the codebase:

  1. libsql (default)     — the Turso client, package name ``libsql``
                            (pip install libsql). NOTE: the older
                            ``libsql-experimental`` / ``libsql-client``
                            packages are DEPRECATED — do not use them.
       • Local dev:  no env vars needed → opens a plain local file
                     (``invoices.db``), fully offline, behaves like sqlite.
       • Cloud:      set TURSO_DATABASE_URL + TURSO_AUTH_TOKEN → the same
                     local file becomes an EMBEDDED REPLICA: reads are
                     served locally, writes go to the Turso primary and
                     reflect back, so state survives Streamlit Cloud
                     container restarts (the whole point of this change).

  2. stdlib sqlite3       — automatic fallback if the ``libsql`` wheel
                            can't be imported (e.g. no prebuilt wheel for
                            your exact Python/OS), or if you force it with
                            USE_LIBSQL=0. Keeps local dev unblocked no
                            matter what.

WHY THIS DESIGN
  libsql returns plain tuples from fetchone()/fetchall() and has no
  ``row_factory``. The rest of the app relies on sqlite3.Row semantics —
  ``row["column"]``, ``row[0]``, ``dict(row)``, ``row.keys()``. A tiny set
  of proxies below restores exactly that, so every ``with get_db() as
  conn:`` site across tabs/, ai/, services/ keeps working untouched.

ENV VARS
  DB_PATH               local database file           (default ./invoices.db)
  USE_LIBSQL            "1"/"0" — prefer libsql        (default "1")
  TURSO_DATABASE_URL    libsql://<db>.turso.io         (cloud only; enables sync)
  TURSO_AUTH_TOKEN      Turso auth token               (cloud only)
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "./invoices.db")


# ═════════════════════════════════════════════════════════════
# BACKEND SELECTION
# ═════════════════════════════════════════════════════════════

def _env_flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off", "")


USE_LIBSQL  = _env_flag("USE_LIBSQL", "1")

# NOTE: TURSO_DATABASE_URL / TURSO_AUTH_TOKEN are read LAZILY (see _turso_url /
# _turso_token below), NOT captured at import time. On Streamlit Cloud the
# secrets->os.environ bridge in main.py may run AFTER this module is first
# imported; reading them at call time means Turso still activates regardless of
# import order. (USE_LIBSQL stays at import time only because it gates the
# libsql import itself, and it defaults ON — cloud never needs to disable it.)

# Try to import the libsql client. If it isn't installed / has no wheel for
# this platform, we silently fall back to stdlib sqlite3 so nothing breaks.
_LIBSQL = None
if USE_LIBSQL:
    try:
        import libsql as _LIBSQL  # noqa: N816  (Turso's package, v0.1.11+)
    except Exception as _imp_err:  # pragma: no cover - depends on env
        _LIBSQL = None
        print(f"ℹ️  libsql not available ({_imp_err.__class__.__name__}); "
              f"using stdlib sqlite3.")


def _turso_url() -> str:
    return os.getenv("TURSO_DATABASE_URL", "").strip()


def _turso_token() -> str:
    return os.getenv("TURSO_AUTH_TOKEN", "").strip()


def _using_libsql() -> bool:
    return _LIBSQL is not None


def sync_enabled() -> bool:
    """True when we're a Turso embedded replica (cloud), i.e. a remote
    primary is configured. Used to decide when to pull remote state."""
    return _using_libsql() and bool(_turso_url())


def backend_name() -> str:
    """Human-readable backend label, handy for a diagnostics panel."""
    if not _using_libsql():
        return "sqlite3 (stdlib, local file)"
    return "libsql embedded replica (Turso)" if sync_enabled() else "libsql (local file)"


# ═════════════════════════════════════════════════════════════
# ROW-COMPATIBILITY SHIM
# Restores sqlite3.Row semantics over libsql's plain-tuple rows so that
# NOTHING downstream has to change: row["col"], row[0], dict(row), keys().
# ═════════════════════════════════════════════════════════════

class _Row:
    __slots__ = ("_cols", "_vals", "_map")

    def __init__(self, cols, vals):
        self._cols = cols
        self._vals = vals
        self._map = None  # built lazily on first keyed access

    def _mapping(self):
        if self._map is None:
            self._map = {c: v for c, v in zip(self._cols, self._vals)}
        return self._map

    def __getitem__(self, key):
        # Integer / slice → positional (like sqlite3.Row);  str → by column name
        if isinstance(key, (int, slice)):
            return self._vals[key]
        return self._mapping()[key]

    def get(self, key, default=None):
        return self._mapping().get(key, default)

    def keys(self):
        return list(self._cols)

    def __iter__(self):
        # tuple(row) / unpacking iterate values, matching sqlite3.Row
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __contains__(self, key):
        return key in self._mapping()

    def __eq__(self, other):
        if isinstance(other, _Row):
            return self._vals == other._vals and list(self._cols) == list(other._cols)
        return NotImplemented

    def __repr__(self):
        return f"Row({self._mapping()!r})"


class _CursorProxy:
    """Wraps a libsql cursor so fetch* return _Row objects.
    Everything else (description, lastrowid, rowcount, close) passes through."""

    def __init__(self, cur):
        self._cur = cur

    @property
    def _cols(self):
        desc = self._cur.description
        return [d[0] for d in desc] if desc else []

    def fetchone(self):
        r = self._cur.fetchone()
        return None if r is None else _Row(self._cols, r)

    def fetchall(self):
        cols = self._cols
        return [_Row(cols, r) for r in self._cur.fetchall()]

    def fetchmany(self, size=None):
        rows = self._cur.fetchmany(size) if size is not None else self._cur.fetchmany()
        cols = self._cols
        return [_Row(cols, r) for r in rows]

    def __iter__(self):
        cols = self._cols
        for r in self._cur:
            yield _Row(cols, r)

    def __getattr__(self, name):
        # description, lastrowid, rowcount, arraysize, close, ...
        return getattr(self._cur, name)


class _ConnProxy:
    """Wraps a libsql connection so .execute() yields row-dict cursors and the
    sqlite3-style surface used across the codebase keeps working. Also usable
    as a context manager (``with _connect() as conn:``) like sqlite3."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        cur = (self._conn.execute(sql, params)
               if params is not None else self._conn.execute(sql))
        return _CursorProxy(cur)

    def executemany(self, sql, seq_of_params):
        return _CursorProxy(self._conn.executemany(sql, seq_of_params))

    def executescript(self, script):
        return self._conn.executescript(script)

    def cursor(self):
        return _CursorProxy(self._conn.cursor())

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def sync(self):
        # Pull latest frames from the Turso primary into the local replica.
        return self._conn.sync()

    def close(self):
        return self._conn.close()

    # Allow `with get_db() as conn:` style even on a bare proxy.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __getattr__(self, name):
        return getattr(self._conn, name)


# ═════════════════════════════════════════════════════════════
# DATABASE SCHEMA  (unchanged — the structure of all tables)
# ═════════════════════════════════════════════════════════════

SCHEMA = """

-- TABLE 1: clients
CREATE TABLE IF NOT EXISTS clients (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    email      TEXT    UNIQUE,
    created_at TEXT    DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT    DEFAULT (datetime('now', 'localtime'))
);

-- TABLE 2: invoices
CREATE TABLE IF NOT EXISTS invoices (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id        INTEGER NOT NULL REFERENCES clients(id),
    invoice_number   TEXT,
    invoice_date     TEXT,
    due_date         TEXT,
    amount           REAL,
    gst_amount       REAL,
    total_amount     REAL,
    currency         TEXT    DEFAULT 'INR',
    status           TEXT    DEFAULT 'unpaid'
                         CHECK(status IN ('unpaid','paid','overdue','partial')),
    description      TEXT,
    confidence       REAL,
    gmail_message_id TEXT    UNIQUE,
    gmail_thread_id  TEXT,
    email_subject    TEXT,
    -- PTP / reminder columns (also added defensively by ptp_intelligence
    -- .ensure_schema() for DBs created before v3.3, but included here so a
    -- fresh database — e.g. a new Turso primary — has them from the start).
    original_due_date     TEXT,
    latest_due_date       TEXT,
    extension_count       INTEGER DEFAULT 0,
    expected_payment_date TEXT,
    reminder_date         TEXT,
    created_at       TEXT    DEFAULT (datetime('now', 'localtime')),
    updated_at       TEXT    DEFAULT (datetime('now', 'localtime'))
);

-- TABLE 3: email_drafts
CREATE TABLE IF NOT EXISTS email_drafts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id   INTEGER NOT NULL REFERENCES invoices(id),
    client_id    INTEGER NOT NULL REFERENCES clients(id),
    to_email     TEXT    NOT NULL,
    cc_email     TEXT,
    subject      TEXT    NOT NULL,
    body         TEXT    NOT NULL,
    status       TEXT    DEFAULT 'pending'
                     CHECK(status IN ('pending','approved','rejected','sent','failed')),
    template_used       TEXT,
    scheduled_send_date TEXT,
    reviewed_by         TEXT,
    created_at   TEXT    DEFAULT (datetime('now', 'localtime')),
    reviewed_at  TEXT,
    sent_at      TEXT
);

-- TABLE 4: push_subscriptions
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint   TEXT    NOT NULL UNIQUE,
    p256dh     TEXT    NOT NULL,
    auth       TEXT    NOT NULL,
    created_at TEXT    DEFAULT (datetime('now', 'localtime'))
);

-- TABLE 5: agent_runs
CREATE TABLE IF NOT EXISTS agent_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at             TEXT    DEFAULT (datetime('now', 'localtime')),
    emails_scanned     INTEGER DEFAULT 0,
    invoices_found     INTEGER DEFAULT 0,
    invoices_stored    INTEGER DEFAULT 0,
    drafts_created     INTEGER DEFAULT 0,
    status             TEXT    DEFAULT 'success',
    error_message      TEXT,
    duration_seconds   REAL
);

-- INDEXES
CREATE INDEX IF NOT EXISTS idx_invoices_due_date  ON invoices(due_date);
CREATE INDEX IF NOT EXISTS idx_invoices_status    ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_invoices_client    ON invoices(client_id);
CREATE INDEX IF NOT EXISTS idx_drafts_status      ON email_drafts(status);
CREATE INDEX IF NOT EXISTS idx_drafts_invoice     ON email_drafts(invoice_id);
"""


# ═════════════════════════════════════════════════════════════
# CONNECTION HELPER  (same public API: `with get_db() as conn:`)
# ═════════════════════════════════════════════════════════════

def _connect(db_path: str):
    """Open a raw connection using whichever backend is active, wrapped so the
    sqlite3.Row-style surface is preserved. Not a context manager itself —
    get_db() handles lifecycle."""
    if _using_libsql():
        if sync_enabled():
            raw = _LIBSQL.connect(db_path, sync_url=_turso_url(),
                                  auth_token=_turso_token())
        else:
            raw = _LIBSQL.connect(db_path)
        conn = _ConnProxy(raw)
        # Enforce table relationships. Best-effort: never crash if a backend
        # variant doesn't accept the PRAGMA.
        try:
            conn.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass
        return conn

    # ---- stdlib sqlite3 fallback (original behaviour, byte-for-byte) ----
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


import threading  # noqa: E402

# Per-thread cache of live libsql connections. Connecting a Turso embedded
# replica is expensive (session + sync with the remote primary), and a single
# page render calls get_db() dozens of times, so we reuse one connection per
# thread instead of reconnecting every call. Thread-local => background scan /
# send threads each get their own; no connection is ever shared across threads.
_conn_cache = threading.local()


def _cache_map() -> dict:
    m = getattr(_conn_cache, "map", None)
    if m is None:
        m = _conn_cache.map = {}
    return m


def _cached_conn(db_path: str):
    m = _cache_map()
    conn = m.get(db_path)
    if conn is None:
        conn = m[db_path] = _connect(db_path)
    return conn


def _drop_cached(db_path: str):
    """Forget (and close) this thread's cached connection for db_path, so the
    next get_db() rebuilds a clean one. Used when a connection may be poisoned."""
    m = _cache_map()
    conn = m.pop(db_path, None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def close_all_connections():
    """Close and forget this thread's cached connections (tests / shutdown)."""
    m = _cache_map()
    for c in list(m.values()):
        try:
            c.close()
        except Exception:
            pass
    _conn_cache.map = {}


@contextmanager
def get_db(path: str = None):
    """
    Opens a database connection. 'with get_db() as conn:' is the one entry point
    used everywhere. On libsql it REUSES a per-thread connection (fast on Turso);
    on the stdlib sqlite3 fallback it opens/closes per call (already cheap).

    SAFETY (this is what makes reuse safe after the earlier Turso crash): on a
    reused connection we (a) roll back to a clean, transaction-free state after
    every successful use, and (b) drop the connection entirely if an operation
    raised — so a poisoned/aborted transaction can never carry forward into the
    next caller. Writers still commit explicitly; reads need no commit.
    """
    db_path = path or DB_PATH

    if _using_libsql():
        conn = _cached_conn(db_path)
        try:
            yield conn
        except Exception:
            _drop_cached(db_path)          # may be poisoned — rebuild next time
            raise
        else:
            try:
                conn.rollback()            # clear any lingering/implicit txn
            except Exception:
                _drop_cached(db_path)
        return

    # ---- stdlib sqlite3 fallback: cheap to (re)connect, keep per-call ----
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def init_db(path: str = None):
    """Creates all tables if they don't already exist.

    On a Turso embedded replica (cloud), we pull the latest state down from
    the primary FIRST, so a freshly-started container with an empty local
    replica file doesn't shadow real data. The CREATE TABLE IF NOT EXISTS
    statements are then no-ops when the tables already exist upstream.
    """
    db_path = path or DB_PATH
    with get_db(db_path) as conn:
        if sync_enabled():
            try:
                conn.sync()
            except Exception as e:  # pragma: no cover - network dependent
                print(f"⚠️  Turso initial sync failed (continuing): {e}")
        conn.executescript(SCHEMA)
        conn.commit()
    print(f"✅ Database ready at: {db_path}  [{backend_name()}]")


def sync_now(path: str = None) -> bool:
    """Explicitly pull remote changes into the local replica. No-op (returns
    False) when not running against a Turso primary. Safe to call anytime."""
    if not sync_enabled():
        return False
    try:
        with get_db(path) as conn:
            conn.sync()
        return True
    except Exception as e:  # pragma: no cover - network dependent
        print(f"⚠️  Turso sync failed: {e}")
        return False


def read_sql_df(sql: str, params=(), path: str = None):
    """Backend-agnostic replacement for ``pd.read_sql(sql, sqlite3.connect(...))``.

    pandas' read_sql wants a DBAPI/SQLAlchemy connection, which the libsql proxy
    isn't a perfect match for — and a raw ``sqlite3.connect(file)`` would bypass
    Turso entirely. This runs the query through get_db() (so it hits whatever
    backend is active) and builds the DataFrame from the cursor's description +
    rows. Works identically on libsql, a Turso embedded replica, and stdlib
    sqlite3.
    """
    import pandas as pd
    with get_db(path) as conn:
        cur = conn.execute(sql, params)
        cols = [d[0] for d in (cur.description or [])]
        rows = cur.fetchall()
    return pd.DataFrame([tuple(r) for r in rows], columns=cols)


# ═════════════════════════════════════════════════════════════
# CLIENT OPERATIONS
# ═════════════════════════════════════════════════════════════

def upsert_client(conn, name: str, email: Optional[str]) -> int:
    """
    Insert a new client OR update an existing one.
    'Upsert' = Update if exists, Insert if not.
    Returns the client's ID number.
    """
    if email:
        row = conn.execute("SELECT id FROM clients WHERE email=?", (email,)).fetchone()
        if row:
            conn.execute(
                "UPDATE clients SET name=?, updated_at=datetime('now','localtime') WHERE id=?",
                (name, row["id"])
            )
            return row["id"]
    else:
        row = conn.execute("SELECT id FROM clients WHERE name=?", (name,)).fetchone()
        if row:
            return row["id"]

    conn.execute("INSERT INTO clients (name, email) VALUES (?, ?)", (name, email))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ═════════════════════════════════════════════════════════════
# INVOICE OPERATIONS
# ═════════════════════════════════════════════════════════════

VALID_STATUSES = {"unpaid", "paid", "overdue", "partial"}


def normalize_status(raw_status) -> str:
    """
    Llama/Groq sometimes return status values outside our 4 allowed values.
    Maps anything the AI returns into one of our 4 valid values. Defaults to
    "unpaid" (safe — better to remind than to silently skip a real invoice).
    """
    if not raw_status:
        return "unpaid"

    s = str(raw_status).strip().lower()

    if s in VALID_STATUSES:
        return s

    if any(word in s for word in ["paid", "settled", "cleared", "complete"]):
        if "partial" in s or "part" in s:
            return "partial"
        if "not" in s or "un" in s:
            return "unpaid"
        return "paid"

    if any(word in s for word in ["overdue", "past due", "late"]):
        return "overdue"

    if any(word in s for word in ["partial", "part payment", "partly"]):
        return "partial"

    if any(word in s for word in ["due", "pending", "unpaid", "outstanding",
                                  "n/a", "none", "null", "unknown"]):
        return "unpaid"

    return "unpaid"


def store_invoice(conn, inv: dict) -> tuple:
    """
    Save an extracted invoice to the database.
    Returns (invoice_id, action):
      - 'inserted'  → brand new invoice saved
      - 'updated'   → existing invoice status refreshed
      - 'skipped_*' → duplicate or too uncertain, ignored
    """
    inv["status"] = normalize_status(inv.get("status"))

    if inv.get("confidence") is not None and inv["confidence"] < 0.40:
        return None, "skipped_low_confidence"

    if inv.get("gmail_message_id"):
        existing = conn.execute(
            "SELECT id FROM invoices WHERE gmail_message_id=?",
            (inv["gmail_message_id"],)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE invoices SET status=?, updated_at=datetime('now','localtime') WHERE id=?",
                (inv["status"], existing["id"])
            )
            return existing["id"], "updated"

    client_id = upsert_client(conn, inv["client_name"], inv.get("client_email"))

    if inv.get("invoice_number"):
        existing = conn.execute(
            "SELECT id FROM invoices WHERE invoice_number=? AND client_id=?",
            (inv["invoice_number"], client_id)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE invoices SET status=?, updated_at=datetime('now','localtime') WHERE id=?",
                (inv["status"], existing["id"])
            )
            return existing["id"], "updated"

    conn.execute("""
        INSERT INTO invoices (
            client_id, invoice_number, invoice_date, due_date,
            amount, gst_amount, total_amount, currency,
            status, description, confidence,
            gmail_message_id, gmail_thread_id, email_subject
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        client_id,
        inv.get("invoice_number"),
        inv.get("invoice_date"),
        inv.get("due_date"),
        inv.get("amount"),
        inv.get("gst_amount"),
        inv.get("total_amount"),
        inv.get("currency", "INR"),
        inv["status"],
        inv.get("description"),
        inv.get("confidence"),
        inv.get("gmail_message_id"),
        inv.get("gmail_thread_id"),
        inv.get("email_subject"),
    ))
    invoice_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return invoice_id, "inserted"


def get_invoices_needing_reminder(conn, days: int = 7) -> list:
    """
    Find unpaid/partial/overdue invoices that need a reminder email.
    Includes upcoming (due within N days) AND already-overdue invoices.
    Excludes paid invoices, invoices with a pending/approved draft, and
    invoices with no client email. Handles messy AI date formats in Python.
    """
    future = (datetime.now().date() + timedelta(days=days)).isoformat()  # noqa: F841

    rows = conn.execute("""
        SELECT
            i.*,
            c.name  AS client_name,
            c.email AS client_email,
            CAST(julianday(i.due_date) - julianday('now') AS INTEGER) AS days_until_due
        FROM invoices i
        JOIN clients c ON c.id = i.client_id
        WHERE i.status IN ('unpaid', 'partial', 'overdue')
          AND i.due_date IS NOT NULL
          AND c.email IS NOT NULL
          AND c.email != ''
          AND i.id NOT IN (
              SELECT invoice_id FROM email_drafts
              WHERE status IN ('pending', 'approved')
          )
        ORDER BY i.due_date ASC
    """).fetchall()

    today     = datetime.now().date()
    future_dt = today + timedelta(days=days)
    result    = []

    for row in rows:
        inv = dict(row)
        raw_due = inv.get("due_date", "")

        due_date = None
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%B %d, %Y", "%d %B %Y"):
            try:
                due_date = datetime.strptime(str(raw_due)[:20].strip(), fmt).date()
                break
            except ValueError:
                continue

        if due_date is None:
            print(f"    ⚠️  Could not parse due_date '{raw_due}' for invoice "
                  f"#{inv.get('invoice_number')} — including in reminders")
            inv["days_until_due"] = 0
            result.append(inv)
            continue

        if due_date <= future_dt:
            inv["days_until_due"] = (due_date - today).days
            result.append(inv)

    return result


def get_all_invoices(conn) -> list:
    """Fetch all invoices for the dashboard table."""
    rows = conn.execute("""
        SELECT
            i.*,
            c.name  AS client_name,
            c.email AS client_email,
            CAST(julianday(i.due_date) - julianday('now') AS INTEGER) AS days_until_due
        FROM invoices i
        JOIN clients c ON c.id = i.client_id
        ORDER BY
            CASE i.status
                WHEN 'overdue'  THEN 1
                WHEN 'partial'  THEN 2
                WHEN 'unpaid'   THEN 3
                WHEN 'paid'     THEN 4
            END,
            i.due_date ASC NULLS LAST
    """).fetchall()
    return [dict(r) for r in rows]


# ═════════════════════════════════════════════════════════════
# EMAIL DRAFT OPERATIONS
# ═════════════════════════════════════════════════════════════

def save_draft(conn, draft: dict) -> int:
    """Save a generated email draft for user review."""
    conn.execute("""
        INSERT INTO email_drafts
            (invoice_id, client_id, to_email, cc_email, subject, body, status)
        VALUES (?, ?, ?, ?, ?, ?, 'pending')
    """, (
        draft["invoice_id"],
        draft["client_id"],
        draft["to_email"],
        draft.get("cc_email"),
        draft["subject"],
        draft["body"],
    ))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_pending_drafts(conn) -> list:
    """Get all drafts waiting for user approval."""
    rows = conn.execute("""
        SELECT
            d.*,
            c.name  AS client_name,
            i.invoice_number,
            i.due_date,
            i.total_amount,
            i.amount,
            i.currency,
            CAST(julianday(i.due_date) - julianday('now') AS INTEGER) AS days_until_due
        FROM email_drafts d
        JOIN invoices i ON i.id = d.invoice_id
        JOIN clients  c ON c.id = d.client_id
        WHERE d.status = 'pending'
        ORDER BY i.due_date ASC
    """).fetchall()
    return [dict(r) for r in rows]


def approve_draft(conn, draft_id: int):
    """Mark a draft as approved (user said YES)."""
    conn.execute("""
        UPDATE email_drafts
        SET status='approved', reviewed_at=datetime('now','localtime')
        WHERE id=?
    """, (draft_id,))
    conn.commit()


def reject_draft(conn, draft_id: int):
    """Mark a draft as rejected (user said NO)."""
    conn.execute("""
        UPDATE email_drafts
        SET status='rejected', reviewed_at=datetime('now','localtime')
        WHERE id=?
    """, (draft_id,))
    conn.commit()


def mark_draft_sent(conn, draft_id: int):
    """Mark a draft as sent after the email is dispatched."""
    conn.execute("""
        UPDATE email_drafts
        SET status='sent', sent_at=datetime('now','localtime')
        WHERE id=?
    """, (draft_id,))
    conn.commit()


def mark_draft_failed(conn, draft_id: int):
    """Mark a draft as failed if sending broke."""
    conn.execute("""
        UPDATE email_drafts SET status='failed' WHERE id=?
    """, (draft_id,))
    conn.commit()


# ═════════════════════════════════════════════════════════════
# PUSH SUBSCRIPTION OPERATIONS
# ═════════════════════════════════════════════════════════════

def save_push_subscription(conn, endpoint: str, p256dh: str, auth: str):
    """Save browser push notification credentials."""
    conn.execute("""
        INSERT INTO push_subscriptions (endpoint, p256dh, auth)
        VALUES (?, ?, ?)
        ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth
    """, (endpoint, p256dh, auth))
    conn.commit()


def get_all_push_subscriptions(conn) -> list:
    """Get all registered push subscribers."""
    rows = conn.execute("SELECT * FROM push_subscriptions").fetchall()
    return [dict(r) for r in rows]


def delete_push_subscription(conn, endpoint: str):
    """Remove a push subscription (when browser unsubscribes)."""
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
    conn.commit()


# ═════════════════════════════════════════════════════════════
# SUMMARY & STATS
# ═════════════════════════════════════════════════════════════

def get_summary(conn) -> dict:
    """Get counts and totals for the dashboard header cards."""
    today = datetime.now().date()
    soon  = (today + timedelta(days=7)).isoformat()
    today = today.isoformat()

    def q(sql, *args):
        return conn.execute(sql, args).fetchone()[0]

    return {
        "total":              q("SELECT COUNT(*) FROM invoices"),
        "unpaid":             q("SELECT COUNT(*) FROM invoices WHERE status IN ('unpaid','partial')"),
        "paid":               q("SELECT COUNT(*) FROM invoices WHERE status='paid'"),
        "overdue":            q("SELECT COUNT(*) FROM invoices WHERE status IN ('unpaid','partial','overdue') AND date(due_date) < date('now')"),
        "due_soon":           q("SELECT COUNT(*) FROM invoices WHERE status IN ('unpaid','partial') AND date(due_date) BETWEEN date(?) AND date(?)", today, soon),
        "pending_drafts":     q("SELECT COUNT(*) FROM email_drafts WHERE status='pending'"),
        "total_outstanding":  q("SELECT COALESCE(SUM(COALESCE(total_amount,amount)),0) FROM invoices WHERE status IN ('unpaid','partial','overdue')"),
        "clients_count":      q("SELECT COUNT(*) FROM clients"),
        "reminders_sent":     q("SELECT COUNT(*) FROM email_drafts WHERE status='sent'"),
    }


# ═════════════════════════════════════════════════════════════
# AGENT RUN LOGGING
# ═════════════════════════════════════════════════════════════

def log_run(conn, data: dict):
    """Save a record of this pipeline run to the audit log."""
    conn.execute("""
        INSERT INTO agent_runs
            (emails_scanned, invoices_found, invoices_stored,
             drafts_created, status, error_message, duration_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("emails_scanned", 0),
        data.get("invoices_found", 0),
        data.get("invoices_stored", 0),
        data.get("drafts_created", 0),
        data.get("status", "success"),
        data.get("error_message"),
        data.get("duration_seconds"),
    ))
    conn.commit()
