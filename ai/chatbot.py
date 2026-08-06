"""
chatbot.py — v3.4

RAG assistant that answers finance questions using the live SQL database
as its knowledge base.

Design (deliberately simple, so it's honest and debuggable)
----------------------------------------------------------
Instead of embedding chunks and doing vector search — which for a
finance dashboard with 82 clients and ~10K invoices is overkill — we
do targeted SQL retrieval based on entities detected in the question:

  1. Extract entities: client names, invoice numbers, statuses, dates,
     KPI keywords from the user's question.
  2. Retrieve: run 3-5 targeted SQL queries against invoices.db AND
     Copy_of_Sales_Data-dummy.xlsx (via excel_data_source) to build a
     compact factual context.
  3. Answer: send {question, context, chat_history} to the LLM with a
     strict "answer only from context" system prompt.

This is proper RAG at the granularity that matches the data.

LLM selection: Ollama (llama3.2) locally; Groq API fallback when
Ollama isn't reachable and GROQ_API_KEY is set.
"""
from __future__ import annotations

import json
import os
import re
import base64
from functools import lru_cache
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from core.database import DB_PATH, get_db


@lru_cache(maxsize=1)
def _bot_avatar_uri() -> str:
    """Base64 data URI for the chatbot avatar (assets/cfo_bot_avatar.jpg).
    Returns '' if the asset isn't found, so callers fall back to the emoji."""
    here = os.path.dirname(os.path.abspath(__file__))            # .../ai
    for path in (os.path.join(here, "..", "assets", "cfo_bot_avatar.jpg"),
                 os.path.join(os.getcwd(), "assets", "cfo_bot_avatar.jpg")):
        try:
            with open(path, "rb") as f:
                return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
        except Exception:
            continue
    return ""

OLLAMA_HOST  = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

SYSTEM_PROMPT = """You are the CFO Copilot assistant for Infrabeat Technologies.
Answer ONLY from the CONTEXT block below. If the context does not contain
the answer, say so honestly — never guess numbers, invoice IDs, or dates.
Keep answers concise (2-4 sentences unless a table is requested).
Currency is INR. All ₹ amounts in the CONTEXT are ALREADY formatted
(Cr = crore = ₹10,000,000; L = lakh = ₹100,000). Quote the ₹ figures exactly
as written in the context — do NOT recompute, rescale, or convert them, and do
not invent a "Cr" value from a raw number.
"""


def _inr(n) -> str:
    """Format an INR amount exactly as the dashboard KPI tiles do (Cr / L / K),
    so the assistant echoes the same figure the user sees on screen."""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "₹0"
    a = abs(n)
    if a >= 1e7:
        return f"₹{n / 1e7:,.2f} Cr"
    if a >= 1e5:
        return f"₹{n / 1e5:,.2f} L"
    if a >= 1e3:
        return f"₹{n / 1e3:,.1f} K"
    return f"₹{n:,.0f}"

# ═══════════════════════════════════════════════════════════════════════════
#  1. RETRIEVAL
# ═══════════════════════════════════════════════════════════════════════════
KPI_KEYWORDS = {
    "dso": "dso", "days sales outstanding": "dso",
    "outstanding": "outstanding",
    "overdue": "overdue",
    "on-time": "on_time", "on time": "on_time",
    "p(late)": "risk", "risk": "risk", "high risk": "risk",
    "ptp": "ptp", "promise": "ptp", "promise to pay": "ptp",
    "cash": "cash_projection", "projection": "cash_projection",
    "aging": "aging",
    "concentration": "concentration", "hhi": "concentration",
    "reminder": "reminder", "reminders sent": "reminder",
    "collection": "collection", "effectiveness": "collection",
}


def _detect_intents(q: str) -> list[str]:
    ql = q.lower()
    return sorted({tag for kw, tag in KPI_KEYWORDS.items() if kw in ql})


def _detect_invoice_numbers(q: str) -> list[str]:
    return list(set(re.findall(r"(ITPL/\d{2}-\d{2}/\d+|INV[-/]?\d+)", q, re.I)))


def _detect_client_names(q: str, known_clients: list[str]) -> list[str]:
    """Fuzzy: return every known client whose name shows up (case-insensitive)
    as a substring of the question or vice versa (short client name in Q)."""
    ql = q.lower()
    hits = []
    for name in known_clients:
        n = name.lower()
        if n in ql or (len(n.split()[0]) > 4 and n.split()[0] in ql):
            hits.append(name)
    return hits[:5]


def _retrieve_context(question: str) -> str:
    """Build a compact factual context string from the DB + workbook."""
    parts: list[str] = []

    # ── Cheap portfolio-level snapshot (always useful for KPI questions) ──
    try:
        from core.excel_source import compute_kpis
        k = compute_kpis()
        if k:
            parts.append(
                "PORTFOLIO SNAPSHOT (INR):\n"
                f"  Total outstanding: {_inr(k['total_outstanding'])}\n"
                f"  Open invoices: {k['open_invoices']}\n"
                f"  Overdue amount: {_inr(k['overdue_amount'])} across "
                f"{k['overdue_count']} invoices\n"
                f"  DSO (90d): {k['dso_days']:.1f} days\n"
                f"  On-time rate: {k['on_time_rate']:.1f}%\n"
                f"  Effective # clients: {k['effective_clients']:.1f} of "
                f"{k['unique_clients']} · HHI {k['hhi']:.0f}\n"
            )
    except Exception:
        pass

    intents = _detect_intents(question)
    inv_nums = _detect_invoice_numbers(question)

    # ── Load client list once for name matching ──────────────────────────
    with get_db(DB_PATH) as conn:
        client_rows = conn.execute(
            "SELECT id, name, email FROM clients"
        ).fetchall()
    client_names = [r["name"] for r in client_rows]
    client_by_name = {r["name"].lower(): dict(r) for r in client_rows}

    hit_clients = _detect_client_names(question, client_names)

    # ── Per-invoice detail ────────────────────────────────────────────────
    if inv_nums:
        with get_db(DB_PATH) as conn:
            placeholders = ",".join("?" * len(inv_nums))
            rows = conn.execute(f"""
                SELECT i.invoice_number, c.name AS client, i.amount, i.total_amount,
                       i.currency, i.due_date, i.original_due_date,
                       i.latest_due_date, i.expected_payment_date,
                       i.extension_count, i.status
                  FROM invoices i JOIN clients c ON c.id = i.client_id
                 WHERE i.invoice_number IN ({placeholders})
            """, inv_nums).fetchall()
        if rows:
            parts.append("INVOICE DETAIL:")
            for r in rows:
                r = dict(r)
                amt = r["total_amount"] or r["amount"] or 0
                parts.append(
                    f"  {r['invoice_number']} · {r['client']} · "
                    f"{_inr(amt)} · status={r['status']}\n"
                    f"    Original due: {r['original_due_date'] or r['due_date']}"
                    f" · Latest due: {r['latest_due_date'] or r['due_date']}"
                    f" · PTP date: {r['expected_payment_date'] or '—'}"
                    f" · Extensions: {r['extension_count'] or 0}"
                )

    # ── Per-client detail ─────────────────────────────────────────────────
    for cname in hit_clients:
        cid = client_by_name[cname.lower()]["id"]
        with get_db(DB_PATH) as conn:
            open_inv = conn.execute("""
                SELECT COUNT(*) AS n, COALESCE(SUM(COALESCE(total_amount, amount)), 0) AS sum_
                  FROM invoices WHERE client_id = ? AND status IN ('unpaid','overdue','partial')
            """, (cid,)).fetchone()
            n_reminders = conn.execute(
                "SELECT COUNT(*) FROM email_drafts WHERE client_id=? AND status='sent'",
                (cid,)).fetchone()[0]
            n_ptps = conn.execute(
                "SELECT COUNT(*) FROM ptp_events WHERE client_id=?",
                (cid,)).fetchone()[0]
            avg_ext = conn.execute(
                "SELECT AVG(days_extended) FROM ptp_events WHERE client_id=?",
                (cid,)).fetchone()[0]
            latest_reply = conn.execute("""
                SELECT ai_summary, ai_promised_date, is_ptp, received_at
                  FROM client_replies
                 WHERE client_id=? AND COALESCE(direction,'in') <> 'out'
                 ORDER BY received_at DESC LIMIT 1
            """, (cid,)).fetchone()

        parts.append(
            f"CLIENT · {cname}:\n"
            f"  Open invoices: {open_inv['n']}, "
            f"{_inr(open_inv['sum_'])} outstanding\n"
            f"  Reminders sent: {n_reminders} · PTPs: {n_ptps}"
            f" · avg days extended: {(avg_ext or 0):.1f}\n"
        )
        if latest_reply:
            r = dict(latest_reply)
            parts.append(
                f"  Latest reply ({r['received_at']}): "
                f"{r['ai_summary']} · promised {r['ai_promised_date'] or '—'}"
                f" · {'PTP' if r['is_ptp'] else 'confirmation'}\n"
            )

    # ── PTP portfolio summary ─────────────────────────────────────────────
    if "ptp" in intents:
        try:
            from ai.ptp_intelligence import ptp_summary
            s = ptp_summary()
            parts.append(
                "PORTFOLIO PTP:\n"
                f"  Total PTPs: {s['total_ptps']} · "
                f"repeat offenders (≥3): {s['repeat_offenders']}\n"
                f"  Avg days extended: {s['avg_days_extended']:.1f}\n"
                f"  Top extenders: " +
                ", ".join(f"{t['client']} ({t['total_days_extended']}d)"
                          for t in s['top_extenders'][:5])
            )
        except Exception:
            pass

    # ── ML risk summary ──────────────────────────────────────────────────
    if "risk" in intents:
        try:
            from ai.ml_intelligence import train_model, score_open_invoices
            m = train_model()
            scored = score_open_invoices()
            if m and not scored.empty:
                hi = scored[scored["p_late"] > 0.7]
                el = float((scored["p_late"] * scored["balance"]).sum())
                parts.append(
                    "RISK MODEL:\n"
                    f"  Held-out AUC: {m.cv_metrics['test_auc']:.3f}"
                    f" · Brier: {m.cv_metrics['test_brier']:.3f}\n"
                    f"  Scored {len(scored)} open invoices\n"
                    f"  High-risk (P>70%): {len(hi)} invoices "
                    f"totalling {_inr(hi['balance'].sum())}\n"
                    f"  Portfolio expected loss: {_inr(el)}\n"
                    f"  Top 5 by expected loss:\n"
                )
                top = scored.nlargest(5, "expected_loss")
                for _, r in top.iterrows():
                    parts.append(
                        f"    {r['invoice_number']} · {r['customer_name']} · "
                        f"{_inr(r['balance'])} · P(late) {r['p_late']:.0%}"
                    )
        except Exception:
            pass

    # ── Overdue / aging ──────────────────────────────────────────────────
    if "overdue" in intents or "aging" in intents:
        with get_db(DB_PATH) as conn:
            top_overdue = conn.execute("""
                SELECT i.invoice_number, c.name AS client, i.total_amount,
                       i.due_date, julianday('now') - julianday(i.due_date) AS days_over
                  FROM invoices i JOIN clients c ON c.id = i.client_id
                 WHERE i.status IN ('overdue','unpaid','partial')
                   AND date(i.due_date) < date('now')
                 ORDER BY days_over DESC LIMIT 10
            """).fetchall()
        if top_overdue:
            parts.append("TOP 10 OVERDUE INVOICES (by days overdue):")
            for r in top_overdue:
                parts.append(
                    f"  {r['invoice_number']} · {r['client']} · "
                    f"{_inr(r['total_amount'] or 0)} · "
                    f"{int(r['days_over'])} days overdue"
                )

    return "\n".join(parts) if parts else "No matching data found."


# ═══════════════════════════════════════════════════════════════════════════
#  2. LLM CALL — Ollama, Groq fallback
# ═══════════════════════════════════════════════════════════════════════════
def _call_ollama(prompt: str, timeout: int = 60) -> str | None:
    try:
        import requests
        r = requests.post(
            f"{OLLAMA_HOST}/api/generate", timeout=timeout,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
        )
        r.raise_for_status()
        return r.json().get("response", "").strip() or None
    except Exception:
        return None


def _call_groq(system: str, messages: list[dict],
                 timeout: int = 30) -> str | None:
    if not GROQ_API_KEY:
        return None
    try:
        import requests
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions", timeout=timeout,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                       "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "system", "content": system}, *messages],
                "temperature": 0.2, "max_tokens": 700,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def answer(question: str, chat_history: list[dict] | None = None) -> dict:
    """Public entry: retrieve context, call LLM, return {answer, context,
    engine} for the UI to render."""
    ctx = _retrieve_context(question)
    history = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in (chat_history or [])
    )
    convo_block = f"CONVERSATION SO FAR:\n{history}\n\n" if history else ""
    prompt = (
        f"{SYSTEM_PROMPT}\n\nCONTEXT:\n{ctx}\n\n"
        f"{convo_block}"
        f"USER QUESTION: {question}\n\nASSISTANT:"
    )

    # Try Ollama first
    resp = _call_ollama(prompt)
    engine = "ollama"

    # Fallback to Groq
    if not resp:
        msgs = list(chat_history or [])
        msgs.append({
            "role": "user",
            "content": f"CONTEXT:\n{ctx}\n\nQUESTION: {question}",
        })
        resp = _call_groq(SYSTEM_PROMPT, msgs)
        engine = "groq"

    if not resp:
        return {
            "answer": ("No LLM is reachable right now — start Ollama "
                        "locally (`ollama serve`) or set `GROQ_API_KEY` in .env. "
                        "Below is the raw context I retrieved for your question:\n\n"
                        + ctx[:1500]),
            "context": ctx,
            "engine": "none",
        }
    return {"answer": resp, "context": ctx, "engine": engine}


# ═══════════════════════════════════════════════════════════════════════════
#  3. UI
# ═══════════════════════════════════════════════════════════════════════════
def render_chatbot() -> None:
    """Renders a full chatbot page — used as a sidebar tab in app.py."""
    st.markdown("### 💬 CFO Copilot Assistant")
    st.caption("Ask anything about invoices, clients, reminders, PTPs, "
                "collections, or dashboard KPIs. Answers come from the "
                "live database.")

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    # Suggested questions
    with st.expander("💡 Sample questions", expanded=False):
        st.markdown("""
- What is the total outstanding across all clients?
- How many invoices are overdue and what is the exposure?
- Which client has the most promise-to-pay events?
- Show me the top 5 high-risk invoices.
- What did our latest reply from Beacon Retail say?
- What is our current DSO and on-time rate?
- Explain the P(late) model — why is invoice INV-CSV-002 flagged?
""")

    # Message history
    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("engine") and msg["engine"] != "user":
                st.caption(f"_via {msg['engine']}_")

    # New input
    q = st.chat_input("Ask a question about the CFO data…")
    if q:
        st.session_state.chat_history.append({"role": "user", "content": q})
        with st.chat_message("user"):
            st.markdown(q)
        with st.chat_message("assistant"):
            with st.spinner("Retrieving from database & thinking…"):
                result = answer(
                    q,
                    chat_history=[
                        {k: v for k, v in m.items() if k in ("role", "content")}
                        for m in st.session_state.chat_history[:-1]
                    ],
                )
            st.markdown(result["answer"])
            st.caption(f"_via {result['engine']}_")
            with st.expander("Show retrieved context", expanded=False):
                st.code(result["context"][:3000] or "(empty)", language="text")
        st.session_state.chat_history.append({
            "role":    "assistant",
            "content": result["answer"],
            "engine":  result["engine"],
        })

    if st.session_state.chat_history and st.button("Clear conversation"):
        st.session_state.chat_history = []
        st.rerun()


# ═══════════════════════════════════════════════════════════════════════════
#  4. STICKY FLOATING BOT (v3.5.1)
# ═══════════════════════════════════════════════════════════════════════════
# Design note (why this version is simpler than v3.5):
# Streamlit's DOM structure and default button rendering vary between minor
# versions. Previous approaches (invisible-overlay-on-HTML-div, SVG-as-CSS-
# background) both drifted off-screen or refused to display the avatar. This
# version uses the most bulletproof approach: put the emoji INSIDE the button
# as the label text, and use CSS to size/round the button. Streamlit always
# renders button labels reliably — the emoji cannot fail to appear.

_FLOAT_CSS = """
<style>
  /* Anchor the FAB's stButton wrapper to the bottom-right of the viewport.
     We match on data-testid, which is stable across Streamlit versions. */
  div[data-testid="stButton"]:has(> button[title="CFO Copilot Assistant"]) {
      position: fixed !important;
      bottom: 28px !important;
      right: 28px !important;
      z-index: 9999 !important;
      margin: 0 !important;
      width: 100px !important;
      height: 100px !important;
  }
  /* Fallback for older Streamlit versions without :has() support */
  div.stButton:has(button[title="CFO Copilot Assistant"]) {
      position: fixed !important;
      bottom: 28px !important;
      right: 28px !important;
      z-index: 9999 !important;
      margin: 0 !important;
      width: 100px !important;
      height: 100px !important;
  }

  /* Style the button itself into a round chat bubble.
     Larger + cream-to-gold gradient for high-contrast emoji. */
  button[title="CFO Copilot Assistant"] {
      width: 100px !important;
      height: 100px !important;
      min-height: 100px !important;
      border-radius: 50% !important;
      padding: 0 !important;
      border: 3px solid #14141a !important;
      background: radial-gradient(circle at 30% 30%, #f8f4e3, #d4af37 75%) !important;
      color: #14141a !important;
      font-size: 54px !important;
      line-height: 1 !important;
      box-shadow: 0 10px 32px rgba(0,0,0,0.60),
                  0 0 28px rgba(212,175,55,0.45),
                  inset 0 -3px 8px rgba(0,0,0,0.20) !important;
      cursor: pointer !important;
      transition: transform 120ms ease, box-shadow 120ms ease !important;
      display: flex !important;
      align-items: center !important;
      justify-content: center !important;
  }
  button[title="CFO Copilot Assistant"]:hover {
      transform: scale(1.08) !important;
      box-shadow: 0 12px 38px rgba(0,0,0,0.65),
                  0 0 44px rgba(212,175,55,0.60) !important;
      border-color: #14141a !important;
  }
  /* The button label paragraph — Streamlit wraps text in <p>, override its margin */
  button[title="CFO Copilot Assistant"] p {
      margin: 0 !important;
      font-size: 54px !important;
      line-height: 1 !important;
  }

  /* Dialog styling */
  div[data-testid="stDialog"] div[role="dialog"] {
      max-width: 560px !important;
      background: #14141a !important;
      border: 1px solid rgba(212,175,55,0.25) !important;
  }
  .cfo-bot-header {
      display: flex; align-items: center; gap: 12px;
      padding: 4px 0 10px 0;
      border-bottom: 1px solid rgba(255,255,255,0.06);
      margin-bottom: 12px;
  }
  .cfo-bot-header-avatar {
      width: 44px; height: 44px; border-radius: 50%;
      background: radial-gradient(circle at 30% 30%, #f8f4e3, #d4af37);
      border: 2px solid #14141a;
      display: flex; align-items: center; justify-content: center;
      flex-shrink: 0;
      font-size: 24px;
      box-shadow: 0 2px 10px rgba(212,175,55,0.30);
  }
  .cfo-bot-header-name { color: #f0eee5; font-size: 15px; font-weight: 600; margin: 0; }
  .cfo-bot-header-sub  { color: #8a8880; font-size: 11px; margin: 0; }
  .cfo-bot-header-dot  {
      display:inline-block; width:8px; height:8px; border-radius:50%;
      background:#8cb04a; margin-right:6px; vertical-align:middle;
      box-shadow: 0 0 8px rgba(140,176,74,0.6);
  }
</style>
"""


@st.dialog("💬 CFO Desk Assistant", width="large")
def _chat_dialog():
    """Chat panel opened by the floating button. Uses st.dialog (native modal).
    Close with × or Esc; reopen anytime — chat history persists."""

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    # Header with matching emoji avatar (same glyph as the FAB — visual continuity)
    st.markdown(
        "<div class='cfo-bot-header'>"
        "<div class='cfo-bot-header-avatar'>🤖</div>"
        "<div>"
        "<div class='cfo-bot-header-name'>CFO Desk</div>"
        "<div class='cfo-bot-header-sub'>"
        "<span class='cfo-bot-header-dot'></span>Live · asks the SQL directly"
        "</div>"
        "</div></div>",
        unsafe_allow_html=True,
    )

    with st.expander("💡 Try asking…", expanded=(len(st.session_state.chat_history) == 0)):
        st.caption(
            "• Total outstanding across all clients?  \n"
            "• Overdue invoices and their exposure?  \n"
            "• Top 5 high-risk invoices?  \n"
            "• Latest reply from <client name>?  \n"
            "• Current DSO and on-time rate?  \n"
            "• Why is invoice INV-9007 flagged?"
        )

    for msg in st.session_state.chat_history[-30:]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("engine") and msg["engine"] not in ("user", None):
                st.caption(f"_via {msg['engine']}_")

    q = st.chat_input("Ask about invoices, PTPs, KPIs…", key="fab_chat_input")
    if q:
        st.session_state.chat_history.append({"role": "user", "content": q})
        with st.chat_message("user"):
            st.markdown(q)
        with st.chat_message("assistant"):
            with st.spinner("Thinking…"):
                result = answer(
                    q,
                    chat_history=[
                        {k: v for k, v in m.items() if k in ("role", "content")}
                        for m in st.session_state.chat_history[:-1]
                    ],
                )
            st.markdown(result["answer"])
            st.caption(f"_via {result['engine']}_")
        st.session_state.chat_history.append({
            "role":    "assistant",
            "content": result["answer"],
            "engine":  result["engine"],
        })

    if st.session_state.chat_history:
        if st.button("🗑 Clear conversation", use_container_width=True,
                     key="fab_clear_chat"):
            st.session_state.chat_history = []
            st.rerun()


def render_floating_bot() -> None:
    """Sticky floating chatbot avatar in the bottom-right of every page.

    Implementation (v3.5.2): uses streamlit-float for positioning. The library
    handles fixed-position + z-index reliably across Streamlit versions, which
    manual CSS selectors were struggling with (Streamlit renames data-testids
    and adds inline styles that outrank ours).

    We only style the button's OWN appearance (size, shape, color) — the
    library handles WHERE it sits. That's a much smaller CSS surface and can't
    silently break when Streamlit updates.

    To change the icon, edit the emoji in the st.button() call below.
    Ready-to-use alternatives: 🤖 💬 🧠 ✨ 🎯 📊

    Requires: pip install streamlit-float
    """
    # Style just the button. We match on the button's `title` (help text)
    # attribute which streamlit-float preserves.
    _av = _bot_avatar_uri()
    _img_bg = f"#0b0b14 url('{_av}') center/cover no-repeat" if _av else None
    _fab_css = """
        <style>
          .st-key-cfo-bot-open { width: 120px !important; }
          .st-key-cfo-bot-open button,
          button[title="CFO Copilot Assistant"] {
              width: 120px !important;
              height: 120px !important;
              min-height: 120px !important;
              border-radius: 50% !important;
              padding: 0 !important;
              border: 3px solid #14141a !important;
              background: __FAB_BG__ !important;
              color: __FAB_EMOJI_COLOR__ !important;
              font-size: __FAB_EMOJI_SIZE__ !important;
              line-height: 1 !important;
              box-shadow: 0 12px 36px rgba(0,0,0,0.62),
                          0 0 30px rgba(110,130,255,0.45),
                          inset 0 -3px 8px rgba(0,0,0,0.20) !important;
              cursor: pointer !important;
              transition: transform 140ms ease, box-shadow 140ms ease !important;
              display: flex !important;
              align-items: center !important;
              justify-content: center !important;
          }
          .st-key-cfo-bot-open button:hover,
          button[title="CFO Copilot Assistant"]:hover {
              transform: scale(1.07) !important;
              box-shadow: 0 14px 42px rgba(0,0,0,0.68),
                          0 0 52px rgba(130,120,255,0.60) !important;
          }
          .st-key-cfo-bot-open button p,
          button[title="CFO Copilot Assistant"] p {
              margin: 0 !important;
              font-size: __FAB_EMOJI_SIZE__ !important;
              line-height: 1 !important;
          }
          /* Dialog stays styled the same way as before */
          div[data-testid="stDialog"] div[role="dialog"] {
              max-width: 560px !important;
              background: #14141a !important;
              border: 1px solid rgba(212,175,55,0.25) !important;
          }
          /* Chat header — styled here (was in an unused block) with readable contrast */
          .cfo-bot-header {
              display:flex; align-items:center; gap:12px;
              padding:4px 0 10px 0; border-bottom:1px solid rgba(255,255,255,0.08);
              margin-bottom:12px;
          }
          .cfo-bot-header-avatar {
              width:44px; height:44px; border-radius:50%;
              background:__HDR_BG__;
              border:2px solid #14141a; display:flex; align-items:center;
              justify-content:center; flex-shrink:0; font-size:__HDR_EMOJI_SIZE__;
              box-shadow:0 2px 10px rgba(110,130,255,0.35);
          }
          .cfo-bot-header-name { color:#f5f3ea !important; font-size:15px; font-weight:600; margin:0; }
          .cfo-bot-header-sub  { color:#b8b4a8 !important; font-size:11.5px; margin:0; }
          .cfo-bot-header-dot  {
              display:inline-block; width:8px; height:8px; border-radius:50%;
              background:#8cb04a; margin-right:6px; vertical-align:middle;
              box-shadow:0 0 8px rgba(140,176,74,0.6);
          }
          /* Readable body text inside the chat dialog */
          div[data-testid="stDialog"] [data-testid="stCaptionContainer"] p,
          div[data-testid="stDialog"] [data-testid="stCaptionContainer"] {
              color:#bbb7ac !important; line-height:1.65 !important;
          }
          div[data-testid="stDialog"] [data-testid="stChatInput"] textarea::placeholder {
              color:#928f86 !important;
          }
        </style>
    """
    _fab_css = (_fab_css
        .replace("__FAB_BG__", _img_bg or "radial-gradient(circle at 30% 30%, #f8f4e3, #d4af37 75%)")
        .replace("__FAB_EMOJI_SIZE__", "0" if _av else "60px")
        .replace("__FAB_EMOJI_COLOR__", "transparent" if _av else "#14141a")
        .replace("__HDR_BG__", _img_bg or "radial-gradient(circle at 30% 30%, #f8f4e3, #d4af37)")
        .replace("__HDR_EMOJI_SIZE__", "0" if _av else "24px"))
    st.markdown(_fab_css, unsafe_allow_html=True)

    # streamlit-float: initialise + wrap button in a floated container
    try:
        from streamlit_float import float_init, float_css_helper
    except ImportError:
        # Fall back to a normal button if the library isn't installed.
        # User sees an inline notice with the install command.
        st.warning(
            "The floating chatbot needs `streamlit-float`. Install it: "
            "`pip install streamlit-float` and restart Streamlit."
        )
        if st.button("🤖 Ask CFO Copilot", key="cfo-bot-open-fallback",
                     help="CFO Copilot Assistant"):
            _chat_dialog()
        return

    float_init(theme=False, include_unstable_primary=False)

    container = st.container()
    with container:
        if st.button("🤖",
                     key="cfo-bot-open",
                     help="CFO Copilot Assistant"):
            _chat_dialog()

    # Float the container to bottom-right, above everything else
    container.float(
        float_css_helper(
            width="120px",
            height="120px",
            bottom="28px",
            right="28px",
            z_index="9999",
            transition=0,
        )
    )
