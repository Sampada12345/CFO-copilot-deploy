"""
auth.py — Restricted signup/login for CFO Copilot (Streamlit).

v3.7 — MIGRATED TO SUPABASE / POSTGRES
────────────────────────────────────────
Previously this module owned its own SQLite file (auth.db) and used the
stdlib sqlite3 driver directly.  With the app-wide move to Supabase, it
now shares the single Postgres pool exposed by core.database.get_db(),
so there's one connection story across the whole codebase.

WHAT CHANGED  (everything else in this file is unchanged from v3.6)
─────────────────────────────────────────────────────────────────────
  • The `_conn()` helper no longer opens a sqlite3 connection — it
    yields a connection-shim from the shared pool.  Callers still use
    `with _conn() as c:` exactly as before; the shim commits on clean
    exit and rolls back on exception, matching the old sqlite3 behaviour.

  • `init_auth_db()` no longer runs CREATE TABLE statements — the
    schema for allowlist / users / login_attempts / password_resets is
    now created by core.database.init_db() as part of the single
    startup schema block.  The function is kept (same name, same
    caller in require_login()) so it can perform the ADMIN_EMAIL
    bootstrap once the tables exist.

  • `_ensure_reset_table()` removed — same reason.

  • The two `INSERT OR REPLACE` statements (in `add_to_allowlist` and
    `issue_reset_code`) are rewritten to explicit
    `INSERT ... ON CONFLICT (email) DO UPDATE SET ...` because the
    Postgres shim intentionally refuses to auto-translate the OR REPLACE
    form (it can't safely infer the conflict target from arbitrary SQL).

  • The bootstrap INSERT OR IGNORE in `init_auth_db()` still works
    verbatim — the shim auto-translates INSERT OR IGNORE INTO ... into
    INSERT INTO ... ON CONFLICT DO NOTHING.

  • Type hints changed from `sqlite3.Row` to `dict | None` — the row
    objects the shim returns are duck-compatible with sqlite3.Row
    (row["col"] / row[0] / dict(row) / row.keys() all work), so every
    call site that reads `row["email"]` etc. is unchanged.

Design decisions (unchanged from v3.6)
--------------------------------------
- PBKDF2-HMAC-SHA256 with per-user salt, 200k iterations. No plaintext,
  no external dependencies (bcrypt not required).
- Login lockout: 5 failed attempts per email per 15 minutes.
- Roles: 'admin' and 'member'. Admins manage the allowlist and users
  from a panel inside the app (sidebar).
- Bootstrap: the email in env var ADMIN_EMAIL is auto-allowlisted as
  admin, so the very first person can sign up without a chicken-and-egg
  problem.
- Session: streamlit session_state + an idle timeout (default 60 min).

Integration (app/main.py, right after st.set_page_config):

    from app.auth import require_login, logout_button, admin_sidebar_panel
    user = require_login()          # blocks until authenticated
    logout_button()                 # renders in sidebar
    if user["role"] == "admin":
        admin_sidebar_panel()       # allowlist + user management

.env additions:
    ADMIN_EMAIL=cfo@yourcompany.com
    SESSION_TIMEOUT_MIN=60          # optional
    # SUPABASE_DB_URL is already required by core/database.py; auth
    # reuses that same pool, so no auth-specific env var is needed.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime

import streamlit as st

# Shared Postgres pool.  Every `with _conn() as c:` call below routes
# through this — no more auth.db file, no more stdlib sqlite3.
from core.database import get_db

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
SESSION_TIMEOUT_MIN = int(os.getenv("SESSION_TIMEOUT_MIN", "60"))

PBKDF2_ITERATIONS = 200_000
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_WINDOW_SEC = 15 * 60


# ---------------------------------------------------------------- database
def _conn():
    """Return a context-managed connection from the shared Supabase pool.

    Kept as a named helper (rather than inlining ``get_db()`` at every
    call site) so this file's diff stays small and future backend swaps
    can be made in one place.  Same shape as the old sqlite3 version:
    ``with _conn() as c:`` commits on clean exit, rolls back on exception.
    """
    return get_db()


def init_auth_db() -> None:
    """Bootstrap step: guarantee the configured admin can always sign up.

    The auth tables themselves (allowlist / users / login_attempts /
    password_resets) are created by ``core.database.init_db()`` — this
    function only handles the ADMIN_EMAIL allowlist entry so the first
    admin can register without an existing admin to invite them.

    Safe to call on every request: INSERT OR IGNORE is a no-op if the
    row already exists (the shim translates this to Postgres's
    ``ON CONFLICT DO NOTHING``).
    """
    if not ADMIN_EMAIL:
        return
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO allowlist (email, role, added_by, added_at) "
            "VALUES (?, 'admin', 'bootstrap', ?)",
            (ADMIN_EMAIL, datetime.utcnow().isoformat()),
        )
        c.commit()


# ---------------------------------------------------------------- hashing
def _hash_password(password: str, salt_hex: str) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS
    )
    return dk.hex()


def _verify_password(password: str, salt_hex: str, stored_hash: str) -> bool:
    return hmac.compare_digest(_hash_password(password, salt_hex), stored_hash)


# ---------------------------------------------------------------- core ops
def is_allowlisted(email: str):
    """Return the allowlist row for `email` (dict-like) or None."""
    with _conn() as c:
        return c.execute(
            "SELECT * FROM allowlist WHERE email = ?", (email.lower().strip(),)
        ).fetchone()


def signup(email: str, full_name: str, password: str) -> tuple[bool, str]:
    email = email.lower().strip()
    if not email or "@" not in email:
        return False, "Enter a valid email address."
    if len(password) < 10:
        return False, "Password must be at least 10 characters."

    entry = is_allowlisted(email)
    if entry is None:
        return False, (
            "This email is not authorised for access. "
            "Ask an administrator to add you to the approved list."
        )

    with _conn() as c:
        if c.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            return False, "An account already exists for this email — please log in."
        salt = secrets.token_hex(16)
        c.execute(
            "INSERT INTO users (email, full_name, password_hash, salt, role, "
            "status, created_at) VALUES (?,?,?,?,?, 'active', ?)",
            (
                email,
                full_name.strip() or email,
                _hash_password(password, salt),
                salt,
                entry["role"],
                datetime.utcnow().isoformat(),
            ),
        )
        c.commit()
    return True, "Account created — you can log in now."


def _is_locked_out(email: str) -> bool:
    cutoff = time.time() - LOCKOUT_WINDOW_SEC
    with _conn() as c:
        n = c.execute(
            "SELECT COUNT(*) FROM login_attempts "
            "WHERE email = ? AND ts > ? AND success = 0",
            (email, cutoff),
        ).fetchone()[0]
    return n >= MAX_FAILED_ATTEMPTS


def login(email: str, password: str) -> tuple[bool, str, dict | None]:
    email = email.lower().strip()
    if _is_locked_out(email):
        return False, "Too many failed attempts. Try again in 15 minutes.", None

    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        ok = bool(
            row
            and row["status"] == "active"
            and _verify_password(password, row["salt"], row["password_hash"])
        )
        c.execute(
            "INSERT INTO login_attempts (email, ts, success) VALUES (?,?,?)",
            (email, time.time(), int(ok)),
        )
        if ok:
            c.execute(
                "UPDATE users SET last_login = ? WHERE email = ?",
                (datetime.utcnow().isoformat(), email),
            )
        c.commit()

    if not ok:
        return False, "Invalid email or password, or account disabled.", None
    return True, "", {"email": row["email"], "name": row["full_name"], "role": row["role"]}


# ---------------------------------------------------------------- admin ops
def add_to_allowlist(email: str, role: str, added_by: str) -> None:
    """Approve `email` at `role`.  Idempotent: if the email is already
    on the list, updates its role / audit fields to the new values.

    Rewritten from SQLite's ``INSERT OR REPLACE`` to Postgres's explicit
    ``INSERT ... ON CONFLICT (email) DO UPDATE SET ...`` — the shim
    intentionally refuses to auto-translate OR REPLACE because it can't
    safely infer the conflict target from arbitrary SQL.
    """
    with _conn() as c:
        c.execute(
            """
            INSERT INTO allowlist (email, role, added_by, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (email) DO UPDATE SET
                role     = EXCLUDED.role,
                added_by = EXCLUDED.added_by,
                added_at = EXCLUDED.added_at
            """,
            (email.lower().strip(), role, added_by, datetime.utcnow().isoformat()),
        )
        c.commit()


def remove_from_allowlist(email: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM allowlist WHERE email = ?", (email.lower().strip(),))
        c.commit()


def set_user_status(email: str, status: str) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET status = ? WHERE email = ?", (status, email))
        c.commit()


def delete_user(email: str) -> None:
    """Fully remove a user's account. Allowlist entry is kept, so admin can
    re-invite the same email if needed."""
    email = email.lower().strip()
    with _conn() as c:
        c.execute("DELETE FROM users WHERE email = ?", (email,))
        c.execute("DELETE FROM login_attempts WHERE email = ?", (email,))
        c.execute("DELETE FROM password_resets WHERE email = ?", (email,))
        c.commit()


# ---- password reset (admin-issued codes; no SMTP required) ----
def issue_reset_code(email: str, issued_by: str) -> str:
    """Admin generates a one-time reset code. Returned in plain text so the
    admin can share it via any trusted channel (Teams / phone / in person).
    Code expires in 30 minutes; single use.

    Rewritten from ``INSERT OR REPLACE`` to explicit
    ``ON CONFLICT (email) DO UPDATE`` (see add_to_allowlist for the same
    reasoning)."""
    email = email.lower().strip()
    with _conn() as c:
        if not c.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            return ""
        code = secrets.token_hex(4).upper()   # 8-char code
        salt = secrets.token_hex(16)
        c.execute(
            """
            INSERT INTO password_resets
                (email, code_hash, salt, expires_at, issued_by)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (email) DO UPDATE SET
                code_hash  = EXCLUDED.code_hash,
                salt       = EXCLUDED.salt,
                expires_at = EXCLUDED.expires_at,
                issued_by  = EXCLUDED.issued_by
            """,
            (email, _hash_password(code, salt), salt,
             time.time() + 30 * 60, issued_by),
        )
        c.commit()
    return code


def redeem_reset_code(email: str, code: str, new_password: str) -> tuple[bool, str]:
    if len(new_password) < 10:
        return False, "New password must be at least 10 characters."
    email = email.lower().strip()
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM password_resets WHERE email = ?", (email,)
        ).fetchone()
        if not row:
            return False, "No reset code has been issued for this email. Ask an admin to generate one."
        if row["expires_at"] < time.time():
            c.execute("DELETE FROM password_resets WHERE email = ?", (email,))
            c.commit()
            return False, "That reset code has expired. Ask the admin to issue a new one."
        if not hmac.compare_digest(
            _hash_password(code.strip().upper(), row["salt"]), row["code_hash"]
        ):
            return False, "Invalid reset code."
        new_salt = secrets.token_hex(16)
        c.execute(
            "UPDATE users SET salt = ?, password_hash = ? WHERE email = ?",
            (new_salt, _hash_password(new_password, new_salt), email),
        )
        c.execute("DELETE FROM password_resets WHERE email = ?", (email,))
        c.commit()
    return True, "Password updated — please log in with your new password."


def list_allowlist() -> list:
    with _conn() as c:
        return c.execute("SELECT * FROM allowlist ORDER BY email").fetchall()


def list_users() -> list:
    with _conn() as c:
        return c.execute(
            "SELECT email, full_name, role, status, last_login FROM users ORDER BY email"
        ).fetchall()


# ---------------------------------------------------------------- streamlit UI
def _session_expired() -> bool:
    last = st.session_state.get("auth_last_seen")
    return bool(last and time.time() - last > SESSION_TIMEOUT_MIN * 60)


LOGIN_CSS = """
<style>
  /* ═══════════════════════════════════════════════════════════════════
     CRED-inspired: obsidian black, muted cream text, one accent colour,
     generous whitespace, no gradients on the button, no red anywhere.
     Red is reserved for error states in the app.
     ═══════════════════════════════════════════════════════════════════ */

  #MainMenu, footer, header {visibility: hidden;}

  /* Deep obsidian background with a subtle radial highlight — the CRED
     'glossy black' feel without going full noir. Works in both themes. */
  .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
      background:
          radial-gradient(1200px 600px at 50% -10%, #1a1a1f 0%, transparent 60%),
          radial-gradient(900px 500px at 50% 110%, #16161b 0%, transparent 55%),
          #0a0a0d !important;
      color: #e8e6df !important;
  }

  /* ── HERO ─────────────────────────────────────────────────────────── */
  .login-hero { text-align:center; padding: 56px 12px 12px 12px; }
  .login-hero .brand {
      font-size: 34px; font-weight: 500; letter-spacing: 1.2px;
      margin: 0; color: #ffffff;
      font-family: 'Segoe UI', 'Inter', -apple-system, sans-serif;
  }
  .login-hero .brand .mono {          /* was ".gold" — same class works */
      color: #ffffff;
      font-weight: 300;
      letter-spacing: 2px;
  }
  .login-hero .divider {
      width: 32px; height: 1px; background: #3a3a42;
      margin: 22px auto 18px auto;
  }
  .login-hero .tag {
      color: #c9c4b6 !important; font-size: 15px; margin: 0;
      font-weight: 300; letter-spacing: .4px;
      font-family: 'Segoe UI', 'Inter', sans-serif;
  }
  .login-hero .sub {
      color: #6b6a63 !important; font-size: 12px;
      margin-top: 10px; letter-spacing: 3px; text-transform: uppercase;
  }

  /* ── FORM CARD ─────────────────────────────────────────────────────── */
  .login-card {
      background: #111114 !important;
      border: 1px solid #23232a !important;
      border-radius: 16px;
      padding: 32px 34px 24px 34px;
      box-shadow:
          0 1px 0 rgba(255,255,255,.03) inset,
          0 24px 60px rgba(0,0,0,.5);
      max-width: 440px; margin: 32px auto 0 auto;
  }

  /* Field labels — muted cream, small caps */
  .login-card label, .login-card label p,
  .login-card [data-testid="stWidgetLabel"] p {
      color: #8a8880 !important;
      font-weight: 500 !important;
      font-size: 11px !important;
      text-transform: uppercase !important;
      letter-spacing: 1.8px !important;
      margin-bottom: 6px !important;
  }

  /* Input fields — dark neutral, cream text, subtle focus */
  .login-card input,
  .login-card [data-baseweb="input"] input,
  .login-card [data-baseweb="base-input"] input {
      background-color: #1a1a1f !important;
      color: #f0eee5 !important;
      -webkit-text-fill-color: #f0eee5 !important;
      font-size: 15px !important;
      caret-color: #c9a961 !important;
  }
  .login-card [data-baseweb="input"],
  .login-card [data-baseweb="base-input"] {
      background-color: #1a1a1f !important;
      border: 1px solid #2a2a32 !important;
      border-radius: 10px !important;
      transition: border-color .2s ease, box-shadow .2s ease;
  }
  .login-card input::placeholder {
      color: #5a5952 !important; opacity: 1;
  }
  .login-card [data-baseweb="input"]:focus-within,
  .login-card [data-baseweb="base-input"]:focus-within {
      border-color: #c9a961 !important;
      box-shadow: 0 0 0 3px rgba(201, 169, 97, 0.10) !important;
  }

  /* Password reveal icon */
  .login-card [data-baseweb="input"] button {
      color: #6b6a63 !important; background: transparent !important;
  }
  .login-card [data-baseweb="input"] button:hover {
      color: #c9a961 !important;
  }

  /* ── TABS ─────────────────────────────────────────────────────────── */
  .login-card .stTabs [data-baseweb="tab-list"] {
      justify-content: flex-start !important;
      gap: 28px !important;
      border-bottom: 1px solid #23232a !important;
      margin-bottom: 24px !important;
  }
  .login-card .stTabs [data-baseweb="tab"] {
      background: transparent !important;
      padding: 8px 0 !important;
  }
  .login-card .stTabs [data-baseweb="tab"] p {
      color: #6b6a63 !important;
      font-weight: 500 !important;
      font-size: 12px !important;
      letter-spacing: 1.5px !important;
      text-transform: uppercase !important;
  }
  .login-card .stTabs [aria-selected="true"] p {
      color: #f0eee5 !important;
  }
  .login-card .stTabs [data-baseweb="tab-highlight"] {
      background: #c9a961 !important;
      height: 2px !important;
  }

  /* ── PRIMARY BUTTON (Sign In / Create / Reset) ─────────────────── */
  .login-card button[kind="primary"],
  .login-card [data-testid="stFormSubmitButton"] > button {
      background: linear-gradient(180deg, #d9bd7e 0%, #c9a961 100%) !important;
      color: #1a1a1f !important;
      border: none !important;
      border-radius: 10px !important;
      padding: 12px 20px !important;
      font-weight: 600 !important;
      font-size: 13px !important;
      letter-spacing: 1.8px !important;
      text-transform: uppercase !important;
      transition: all .2s ease !important;
      box-shadow: 0 4px 12px rgba(201,169,97,.15) !important;
  }
  .login-card button[kind="primary"]:hover,
  .login-card [data-testid="stFormSubmitButton"] > button:hover {
      transform: translateY(-1px) !important;
      box-shadow: 0 6px 18px rgba(201,169,97,.28) !important;
      background: linear-gradient(180deg, #e0c58a 0%, #d1b06e 100%) !important;
  }

  /* ── ALERTS ───────────────────────────────────────────────────────── */
  .login-card [data-testid="stAlert"] {
      border-radius: 10px !important;
      border: 1px solid #23232a !important;
      background: #16161b !important;
  }
  .login-card [data-testid="stAlert"] p {
      color: #c9c4b6 !important;
      font-size: 13px !important;
  }

  /* ── CAPTION UNDER TABS ──────────────────────────────────────────── */
  .login-card [data-testid="stCaptionContainer"] p,
  .login-card small {
      color: #8a8880 !important;
      font-size: 12px !important;
      line-height: 1.6 !important;
  }

  /* ── FOOTER FEATURE STRIP ─────────────────────────────────────────── */
  .features {
      text-align: center; padding: 40px 12px 24px 12px;
      font-size: 11px; letter-spacing: 2px; text-transform: uppercase;
      font-weight: 500;
  }
  .features span { margin: 0 14px; color: #6b6a63; }
  .features .dot { color: #3a3a42; margin: 0 6px; }
</style>
"""


def require_login() -> dict:
    """Render the login/signup gate. Returns the user dict once authenticated;
    calls st.stop() otherwise, so nothing below it runs for anonymous visitors."""
    init_auth_db()

    if st.session_state.get("auth_user") and not _session_expired():
        st.session_state["auth_last_seen"] = time.time()
        return st.session_state["auth_user"]

    if _session_expired():
        st.session_state.pop("auth_user", None)
        session_expired_flag = True
    else:
        session_expired_flag = False

    st.markdown(LOGIN_CSS, unsafe_allow_html=True)

    # Elegant hero block
    st.markdown(
        """
        <div class='login-hero'>
            <p class='brand'>CFO <span class='mono'>COPILOT</span></p>
            <div class='divider'></div>
            <p class='tag'>Your intelligent partner for cash, collections & clarity.</p>
            <p class='sub'>Accounts Receivable · Intelligence</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Centered card
    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        st.markdown("<div class='login-card'>", unsafe_allow_html=True)
        if session_expired_flag:
            st.warning("Session expired — please log in again.")

        tab_login, tab_signup, tab_forgot = st.tabs(
            ["Sign In", "Create Account", "Forgot Password"]
        )

        with tab_login:
            with st.form("login_form", clear_on_submit=False):
                email = st.text_input("Work email",
                                      placeholder="you@company.com")
                password = st.text_input("Password", type="password",
                                         placeholder="Your password")
                if st.form_submit_button("Sign In", use_container_width=True,
                                         type="primary"):
                    ok, msg, user = login(email, password)
                    if ok:
                        st.session_state["auth_user"] = user
                        st.session_state["auth_last_seen"] = time.time()
                        st.rerun()
                    else:
                        st.error(msg)

        with tab_signup:
            st.caption(
                "Access is invitation-only. Signup works only for emails "
                "an administrator has pre-approved."
            )
            with st.form("signup_form"):
                s_email = st.text_input("Work email", key="su_email")
                s_name = st.text_input("Full name", key="su_name")
                s_pw = st.text_input("Password (min 10 chars)",
                                     type="password", key="su_pw")
                s_pw2 = st.text_input("Confirm password",
                                      type="password", key="su_pw2")
                if st.form_submit_button("Create Account",
                                         use_container_width=True,
                                         type="primary"):
                    if s_pw != s_pw2:
                        st.error("Passwords do not match.")
                    else:
                        ok, msg = signup(s_email, s_name, s_pw)
                        (st.success if ok else st.error)(msg)

        with tab_forgot:
            st.caption(
                "Password reset codes are issued by an administrator "
                "(no email server involved). Ask them to generate one for "
                "you — it's an 8-character code, valid for 30 minutes."
            )
            with st.form("forgot_form"):
                f_email = st.text_input("Your email", key="fp_email")
                f_code  = st.text_input("Reset code from admin",
                                        key="fp_code",
                                        placeholder="e.g. A3F921C0")
                f_pw    = st.text_input("New password (min 10 chars)",
                                        type="password", key="fp_pw")
                f_pw2   = st.text_input("Confirm new password",
                                        type="password", key="fp_pw2")
                if st.form_submit_button("Reset Password",
                                         use_container_width=True,
                                         type="primary"):
                    if f_pw != f_pw2:
                        st.error("Passwords do not match.")
                    else:
                        ok, msg = redeem_reset_code(f_email, f_code, f_pw)
                        (st.success if ok else st.error)(msg)

        st.markdown("</div>", unsafe_allow_html=True)

    # Footer feature strip
    st.markdown(
        """
        <div class='features'>
            <span>Real-time KPIs</span><span class='dot'>·</span>
            <span>AI Reminders</span><span class='dot'>·</span>
            <span>Smart Notifications</span><span class='dot'>·</span>
            <span>Portfolio Risk</span><span class='dot'>·</span>
            <span>Enterprise Access</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.stop()  # never reached when authenticated


def logout_button() -> None:
    user = st.session_state.get("auth_user")
    if not user:
        return
    with st.sidebar:
        st.markdown(f"**{user['name']}**  \n`{user['role']}`")
        if st.button("Log out", use_container_width=True):
            st.session_state.pop("auth_user", None)
            st.rerun()


def admin_sidebar_panel() -> None:
    """Allowlist + user management. Call only for role == 'admin'."""
    with st.sidebar.expander("⚙️ Access management (admin)"):
        st.markdown("**Approved emails**")
        for row in list_allowlist():
            c1, c2 = st.columns([4, 1])
            c1.write(f"{row['email']} · {row['role']}")
            if row["email"] != ADMIN_EMAIL and c2.button("✕", key=f"rm_{row['email']}"):
                remove_from_allowlist(row["email"])
                st.rerun()

        new_email = st.text_input("Add email to allowlist", key="al_new_email")
        new_role = st.selectbox("Role", ["member", "admin"], key="al_new_role")
        if st.button("Approve email") and new_email:
            add_to_allowlist(new_email, new_role, st.session_state["auth_user"]["email"])
            st.success(f"Approved {new_email}")
            st.rerun()

        st.divider()
        st.markdown("**Registered users**")
        for u in list_users():
            c1, c2, c3, c4 = st.columns([4, 1.2, 1.4, 1])
            c1.write(f"{u['email']}  ·  _{u['status']}_")
            is_self = u["email"] == st.session_state["auth_user"]["email"]

            # enable / disable
            if not is_self:
                lbl = "disable" if u["status"] == "active" else "enable"
                if c2.button(lbl, key=f"tg_{u['email']}",
                             use_container_width=True):
                    set_user_status(
                        u["email"],
                        "disabled" if lbl == "disable" else "active"
                    )
                    st.rerun()
            else:
                c2.write("_(you)_")

            # issue reset code
            if c3.button("reset code",
                         key=f"rst_{u['email']}",
                         use_container_width=True,
                         help="Generate a one-time password-reset code. Share it with the user via a trusted channel. Expires in 30 minutes."):
                code = issue_reset_code(u["email"],
                                        st.session_state["auth_user"]["email"])
                if code:
                    st.session_state[f"_show_code_{u['email']}"] = code
                st.rerun()
            if code := st.session_state.get(f"_show_code_{u['email']}"):
                st.info(
                    f"🔑 Code for **{u['email']}** (valid 30 min): `{code}`  "
                    "— share via Teams / phone. Ask them to use the "
                    "*Forgot Password* tab.",
                    icon="🔐",
                )

            # delete user (double-click safeguard via session flag)
            if not is_self:
                flag_key = f"_confirm_del_{u['email']}"
                if c4.button("🗑",
                             key=f"del_{u['email']}",
                             use_container_width=True,
                             help="Delete this user's account entirely. The email stays on the allowlist so they can sign up again."):
                    if st.session_state.get(flag_key):
                        delete_user(u["email"])
                        st.session_state.pop(flag_key, None)
                        st.success(f"Deleted {u['email']}")
                        st.rerun()
                    else:
                        st.session_state[flag_key] = True
                        st.warning(
                            f"Click 🗑 again to confirm deleting **{u['email']}**"
                        )
