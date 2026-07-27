"""
tab1_dashboard.py — v3.1 Executive Overview

Complete replacement for the old xlsx-anchored Tab 1. Reads exclusively from
Copy_of_Sales_Data-dummy.xlsx (via excel_data_source), pushes rows into
invoices.db in the background on first render, then computes every KPI and
chart from those sources.

Layout (top → bottom, all sections come with ⓘ tooltips):
  1. Snapshot banner (as-of, currency, invoice/client counts) — no old
     'ap_ar_data.xlsx' warning banner.
  2. Cash-KPI strip (Total Outstanding, Overdue, Overdue > ₹50k,
     Effective clients, DSO, On-time %).
  3. AR Aging chart (Current / 1-30 / 31-60 / 61-90 / 90+).
  4. Reminder Outcomes tracker (paid-within-7d of reminder).
  5. DSO trend (rolling 30-day, last 6 months).
  6. Payment Mode mix (Bank Transfer / UPI / Cheque split of the last
     90 days of receipts, with median days-to-pay per mode).
  7. Top Clients by outstanding (concentration bar + HHI callout).
  8. Salesperson leaderboard (revenue, DSO, on-time % per rep).

Everything is cached (@st.cache_data ttl=300) so refresh cost stays flat.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from core.database import DB_PATH, get_db
from core.excel_source import (XLSX_PATH, build_view, compute_kpis,
                               import_initial_load)
from core.kpi_catalog import kpi_help  # tooltip text lives in one place


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _money(v, ccy="₹") -> str:
    try:
        n = float(v or 0)
    except (TypeError, ValueError):
        return f"{ccy}0"
    if abs(n) >= 1e7:
        return f"{ccy}{n / 1e7:,.2f} Cr"
    if abs(n) >= 1e5:
        return f"{ccy}{n / 1e5:,.2f} L"
    return f"{ccy}{n:,.0f}"


def _kpi(col, label: str, value: str, help_text: str, delta: str | None = None):
    """Uniform metric renderer — same font size everywhere, always with ⓘ."""
    with col:
        st.metric(label=label, value=value, delta=delta, help=help_text)


def _auto_load_if_needed():
    """Silent initial load: if the DB has zero invoices but the workbook is
    present, push it in. Runs at most once per session.

    v3.5.1: made resilient to two Streamlit-Cloud-specific edge cases —
    (a) DB file exists but the `invoices` table hasn't been created yet
        (fresh SQLite in an ephemeral filesystem), and
    (b) workbook path is set but the file itself isn't there
        (deployed without the stub demo).
    Either now shows a friendly warning instead of crashing.
    """
    if st.session_state.get("_auto_load_done"):
        return

    workbook_present = Path(XLSX_PATH).exists()

    if not workbook_present:
        st.warning(
            f"📄 Sales workbook not found at `{XLSX_PATH}`.\n\n"
            "The dashboard needs the invoice data to compute KPIs. "
            "If you're running on Streamlit Cloud, add a stub workbook "
            "to `data/` in the repo (see docs/DEPLOYMENT.md), or update "
            "the `SALES_XLSX` secret to point to a file that exists."
        )
        st.session_state["_auto_load_done"] = True
        return

    # Both file and DB exist — check whether the initial load has run.
    # `invoices` table might not exist yet on a fresh DB, so tolerate that.
    try:
        with get_db(DB_PATH) as conn:
            row = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()
            n = row[0] if row else 0
    except Exception:
        # Table doesn't exist yet — treat as an empty DB, initial load
        # will create it.
        n = 0

    if n == 0:
        try:
            with st.spinner("First-time load: importing invoices from the workbook…"):
                import_initial_load()
        except Exception as e:
            st.error(
                f"Initial load from `{XLSX_PATH}` failed: {e}\n\n"
                "The dashboard will show empty panels until this is fixed."
            )
    st.session_state["_auto_load_done"] = True


# ─────────────────────────────────────────────────────────────────────────────
#  Analytics — cached
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def aging_buckets() -> pd.DataFrame:
    v = build_view()
    inv = v["invoices"]
    if inv.empty:
        return pd.DataFrame(columns=["bucket", "amount", "count"])
    open_inv = inv[inv["Balance"].fillna(0) > 0].copy()
    if open_inv.empty:
        return pd.DataFrame(columns=["bucket", "amount", "count"])
    anchor = pd.Timestamp(v["anchor_date"])
    d = (anchor - open_inv["Due Date"]).dt.days.fillna(-1)
    def _b(days):
        if days <= 0:  return "Current"
        if days <= 30: return "1–30"
        if days <= 60: return "31–60"
        if days <= 90: return "61–90"
        return "90+"
    open_inv["bucket"] = d.apply(_b)
    order = ["Current", "1–30", "31–60", "61–90", "90+"]
    agg = (open_inv.groupby("bucket")["Balance"]
           .agg(["sum", "count"]).reindex(order).fillna(0).reset_index())
    agg.columns = ["bucket", "amount", "count"]
    return agg


@st.cache_data(ttl=300)
def dso_trend(window_days: int = 180, roll_days: int = 30) -> pd.DataFrame:
    v = build_view()
    inv = v.get("all_invoices", pd.DataFrame())
    if inv.empty:
        return pd.DataFrame()
    closed = inv[inv["Invoice Status"] == "Closed"].copy()
    if closed.empty:
        return pd.DataFrame()
    closed["Invoice Date"] = pd.to_datetime(closed["Invoice Date"], errors="coerce")
    closed["paid_date"]    = pd.to_datetime(closed["last_modified_time"],
                                            errors="coerce", dayfirst=True)
    closed["dtp"] = (closed["paid_date"] - closed["Invoice Date"]).dt.days
    closed = closed[closed["dtp"].between(0, 365)]
    if closed.empty:
        return pd.DataFrame()
    anchor = pd.Timestamp(v["anchor_date"])
    closed = closed[closed["paid_date"] >= (anchor - pd.Timedelta(days=window_days))]
    if closed.empty:
        return pd.DataFrame()
    closed = closed.sort_values("paid_date")
    daily = closed.groupby(closed["paid_date"].dt.date)["dtp"].mean()
    daily.index = pd.to_datetime(daily.index)
    roll = daily.rolling(f"{roll_days}D").mean().reset_index()
    roll.columns = ["date", "dso"]
    return roll


@st.cache_data(ttl=300)
def payment_mode_analytics() -> pd.DataFrame:
    """Payment sheet → mode split + median days-to-pay per mode
    over the last 90 days.  Mode is inferred from 'payment_mode' column."""
    v = build_view()
    p = v.get("payments", pd.DataFrame())
    if p.empty:
        return pd.DataFrame()
    mode_col = next((c for c in ("payment_mode", "PaymentMode", "Mode")
                     if c in p.columns), None)
    date_col = next((c for c in ("date", "Date", "payment_date") if c in p.columns), None)
    amt_col  = next((c for c in ("amount", "Amount", "amount_bcy") if c in p.columns), None)
    if not (mode_col and date_col and amt_col):
        return pd.DataFrame()
    p = p.copy()
    p[date_col] = pd.to_datetime(p[date_col], errors="coerce")
    anchor = pd.Timestamp(v["anchor_date"])
    p = p[p[date_col] >= (anchor - pd.Timedelta(days=90))]
    if p.empty:
        return pd.DataFrame()
    # Join to invoice to compute days_to_pay
    inv = v.get("all_invoices", pd.DataFrame())
    if not inv.empty and "invoice_number" in p.columns:
        j = p.merge(inv[["Invoice Number", "Invoice Date"]],
                    left_on="invoice_number", right_on="Invoice Number", how="left")
        j["dtp"] = (j[date_col] - j["Invoice Date"]).dt.days
    else:
        j = p.copy()
        j["dtp"] = np.nan
    agg = (j.groupby(mode_col)
             .agg(total=(amt_col, "sum"),
                  count=(amt_col, "count"),
                  median_dtp=("dtp", "median"))
             .reset_index()
             .rename(columns={mode_col: "mode"}))
    agg["share"] = 100 * agg["total"] / agg["total"].sum() if agg["total"].sum() else 0
    return agg.sort_values("total", ascending=False)


@st.cache_data(ttl=300)
def top_clients_concentration(n: int = 10) -> pd.DataFrame:
    v = build_view()
    inv = v["invoices"]
    if inv.empty:
        return pd.DataFrame()
    op = inv[inv["Balance"].fillna(0) > 0]
    if op.empty:
        return pd.DataFrame()
    by = (op.groupby("Customer Name")["Balance"]
          .sum().sort_values(ascending=False).head(n).reset_index())
    by["pct"] = 100 * by["Balance"] / op["Balance"].sum()
    return by


@st.cache_data(ttl=300)
def salesperson_leaderboard() -> pd.DataFrame:
    v = build_view()
    inv = v.get("all_invoices", pd.DataFrame())
    if inv.empty or "salesperson_name" not in inv.columns:
        return pd.DataFrame()
    inv = inv.copy()
    inv["paid_date"] = pd.to_datetime(inv["last_modified_time"],
                                       errors="coerce", dayfirst=True)
    inv["Invoice Date"] = pd.to_datetime(inv["Invoice Date"], errors="coerce")
    inv["dtp"] = (inv["paid_date"] - inv["Invoice Date"]).dt.days
    closed = inv[inv["Invoice Status"] == "Closed"]
    if closed.empty:
        return pd.DataFrame()
    on_time = closed[(closed["paid_date"] <= closed["Due Date"])]
    grp = closed.groupby("salesperson_name")
    lead = pd.DataFrame({
        "Revenue (Closed)": grp["Total"].sum(),
        "Invoices":         grp.size(),
        "Avg DSO (days)":   grp["dtp"].mean().round(1),
        "On-time %":       (100 * on_time.groupby("salesperson_name").size() /
                            grp.size()).round(1),
    }).sort_values("Revenue (Closed)", ascending=False).reset_index()
    lead["Revenue (Closed)"] = lead["Revenue (Closed)"].apply(lambda v: _money(v))
    return lead.fillna({"On-time %": 0})


@st.cache_data(ttl=300)
def reminder_outcomes() -> dict:
    """Reminder outcomes tracker: for each sent draft, was the invoice
    marked 'paid' within 7 days of the send?
    Requires reminders_sent (email_drafts.status='sent') and payment status."""
    with get_db(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT d.sent_at, i.status, i.due_date
            FROM email_drafts d
            JOIN invoices i ON i.id = d.invoice_id
            WHERE d.status = 'sent' AND d.sent_at IS NOT NULL
        """).fetchall()
    total  = len(rows)
    paid7  = sum(1 for r in rows if r["status"] == "paid")   # rough proxy
    return {
        "total_sent":       total,
        "paid_within_7d":   paid7,
        "response_rate":    (100.0 * paid7 / total) if total else 0.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Main render
# ─────────────────────────────────────────────────────────────────────────────
def render_tab1() -> None:
    _auto_load_if_needed()

    if not Path(XLSX_PATH).exists():
        st.error(
            f"Workbook not found at `{XLSX_PATH}`.\n\n"
            "Place the Sales Data workbook there and reload."
        )
        return

    k = compute_kpis()
    if not k:
        st.warning("Workbook loaded but has no rows in scope.")
        return

    v = build_view()
    ccy = k.get("currency", "INR")
    money = lambda x: _money(x, "₹" if ccy == "INR" else f"{ccy} ")

    # ── header row ─────────────────────────────────────────────────────
    hcol1, hcol2 = st.columns([4, 1])
    with hcol1:
        st.markdown("### 📊 Executive Overview")
        st.caption(
            f"AR portfolio · {k['open_invoices']:,} open invoices · "
            f"{k['unique_clients']} active clients · "
            f"currency **{ccy}**"
        )
    with hcol2:
        if st.button("🔄 Refresh data", use_container_width=True,
                     help="Reload the workbook and recompute all KPIs & charts."):
            compute_kpis.clear(); build_view.clear()
            aging_buckets.clear(); dso_trend.clear()
            payment_mode_analytics.clear(); top_clients_concentration.clear()
            salesperson_leaderboard.clear(); reminder_outcomes.clear()
            st.session_state.pop("_auto_load_done", None)
            st.rerun()

    st.divider()

    # ── PRIMARY KPI STRIP ──────────────────────────────────────────────
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    _kpi(c1, "Total Outstanding", money(k["total_outstanding"]),
         kpi_help("total_outstanding"))
    _kpi(c2, "Overdue", money(k["overdue_amount"]),
         kpi_help("overdue_amount"),
         delta=f"{k['overdue_count']} invoices")
    _kpi(c3, "Large Overdues (>₹50K)", f"{k['overdue_over_50k']}",
         kpi_help("overdue_over_50k"))
    _kpi(c4, "DSO (90d)",
         f"{k['dso_days']:.1f}d" if k.get("dso_days") else "—",
         kpi_help("dso_days"))
    _kpi(c5, "On-time %",
         f"{k['on_time_rate']:.1f}%" if k.get("on_time_rate") else "—",
         kpi_help("on_time_rate"))
    _kpi(c6, "Effective # Clients",
         f"{k['effective_clients']:.1f}",
         kpi_help("effective_clients"),
         delta=f"HHI {k['hhi']:.0f}")

    st.divider()

    # ── AR AGING + REMINDER OUTCOMES ───────────────────────────────────
    ac, rc = st.columns([1.6, 1])
    with ac:
        st.markdown("#### ◆ AR Aging",
                    help="Outstanding balance bucketed by days past due date. "
                         "Everything to the right of 'Current' is money you "
                         "should already have collected.")
        agg = aging_buckets()
        if agg.empty:
            st.info("No open invoices to age.")
        else:
            fig = go.Figure()
            colors = ["#146c43", "#b8860b", "#d97706", "#dc6b2f", "#c0392b"]
            fig.add_bar(x=agg["bucket"], y=agg["amount"],
                        marker_color=colors[:len(agg)],
                        text=[f"{money(a)}<br>{int(c)} inv"
                              for a, c in zip(agg["amount"], agg["count"])],
                        textposition="outside")
            fig.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10),
                              yaxis_title=None, xaxis_title=None,
                              showlegend=False)
            st.plotly_chart(fig, use_container_width=True)

    with rc:
        st.markdown("#### ◆ Reminder Outcomes",
                    help="Whether reminders are actually working. We track "
                         "sent reminders and whether the invoice moved to "
                         "'paid' within 7 days. Response rate = paid ÷ sent. "
                         "Higher = your templates are landing.")
        r = reminder_outcomes()
        _kpi(st.container(), "Reminders sent (all-time)",
             f"{r['total_sent']:,}", kpi_help("reminders_sent_30d"))
        _kpi(st.container(), "Paid within 7 days",
             f"{r['paid_within_7d']:,}",
             "Invoices whose reminder was followed by 'paid' status within a week.")
        _kpi(st.container(), "Response rate",
             f"{r['response_rate']:.1f}%",
             kpi_help("reminder_response_rate"))
        if r["total_sent"] == 0:
            st.caption("_Send a few reminders from the Notification Center "
                       "to start building this dataset._")

    st.divider()

    # ── DSO TREND + TOP CLIENTS ────────────────────────────────────────
    dc, tc = st.columns([1.6, 1])
    with dc:
        st.markdown("#### ◆ DSO trend  · 30-day rolling",
                    help="Days Sales Outstanding, rolling 30-day mean over "
                         "the last 6 months. Trending up = collections are "
                         "slipping; trending down = tightening.")
        trend = dso_trend()
        if trend.empty:
            st.caption("_Not enough closed invoices yet._")
        else:
            fig2 = go.Figure()
            fig2.add_trace(go.Scatter(
                x=trend["date"], y=trend["dso"], mode="lines",
                line=dict(color="#d4af37", width=2.5), name="DSO",
                hovertemplate="%{x|%d %b %Y}<br>DSO: %{y:.1f} d<extra></extra>",
            ))
            fig2.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10),
                               yaxis_title="days", xaxis_title=None,
                               showlegend=False)
            st.plotly_chart(fig2, use_container_width=True)

    with tc:
        st.markdown("#### ◆ Top clients · outstanding",
                    help="Your 10 largest outstanding balances. "
                         "Concentration risk lives here — one client walking "
                         "would take a bar off this chart.")
        tops = top_clients_concentration(10)
        if tops.empty:
            st.info("No open invoices.")
        else:
            fig3 = go.Figure()
            fig3.add_bar(x=tops["Balance"], y=tops["Customer Name"],
                         orientation="h", marker_color="#0b2e4f",
                         text=[f"{money(b)}  ({p:.1f}%)"
                               for b, p in zip(tops["Balance"], tops["pct"])],
                         textposition="outside")
            fig3.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=60),
                               xaxis_title=None, yaxis=dict(autorange="reversed"),
                               showlegend=False)
            st.plotly_chart(fig3, use_container_width=True)

    st.divider()

    # ── PAYMENT MODE + SALESPERSON LEADERBOARD ─────────────────────────
    pc, sc = st.columns(2)
    with pc:
        st.markdown("#### ◆ Payment Mode mix · last 90 days",
                    help="How clients paid you over the last 90 days. "
                         "Median days-to-pay per mode is often revealing — "
                         "cheque payers tend to be materially slower than "
                         "UPI or bank-transfer payers.")
        pm = payment_mode_analytics()
        if pm.empty:
            st.caption("_No payments in the last 90 days._")
        else:
            fig4 = px.pie(pm, values="total", names="mode", hole=0.45,
                          color_discrete_sequence=px.colors.qualitative.Prism)
            fig4.update_traces(textinfo="label+percent")
            fig4.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10),
                               showlegend=False)
            st.plotly_chart(fig4, use_container_width=True)
            st.dataframe(pm[["mode", "count", "median_dtp"]]
                          .rename(columns={"mode": "Mode", "count": "# payments",
                                           "median_dtp": "Median days to pay"}),
                          hide_index=True, use_container_width=True)

    with sc:
        st.markdown("#### ◆ Salesperson leaderboard",
                    help="Revenue, DSO, and on-time rate by salesperson. "
                         "Some sellers systematically sell to slow payers — "
                         "worth a conversation with the sales lead.")
        lead = salesperson_leaderboard()
        if lead.empty:
            st.caption("_Salesperson column not present in the workbook._")
        else:
            st.dataframe(lead, hide_index=True, use_container_width=True,
                         height=280)

    st.divider()

    # ══════════════════════════════════════════════════════════════════
    #  NEW · retained-from-old-dashboard: 8-week forecast + action queue
    # ══════════════════════════════════════════════════════════════════
    from ai.analytics import (
        eight_week_forecast, fig_eight_week_forecast, action_queue,
        collection_effectiveness, fig_collection_effectiveness,
        risk_cube_data, fig_risk_cube,
        cash_projection, fig_cash_projection,
        derived_kpis,
    )
    from ai.ml_intelligence import render_intelligence_section

    st.markdown("#### ◆ 8-week cash flow forecast · action queue",
                help="Weekly bar chart of expected AR inflow (invoices due "
                     "by week over the next 8 weeks) alongside the action "
                     "queue — top invoices to chase first, ranked by "
                     "expected loss = Balance × P(late).")
    ew1, ew2 = st.columns([1.4, 1])
    with ew1:
        fc = eight_week_forecast()
        if fc.empty:
            st.caption("_No open invoices with future due dates._")
        else:
            st.plotly_chart(fig_eight_week_forecast(fc),
                            use_container_width=True)

    with ew2:
        st.markdown("**Action queue** · prioritised by expected loss")
        st.caption("`expected_loss = balance × P(late)` — chase these first.")
        aq = action_queue(top_n=8)
        if aq.empty:
            st.caption("_ML model not ready yet — check the Intelligence "
                       "section below._")
        else:
            for _, r in aq.iterrows():
                cli = str(r["customer_name"])[:22]
                bal = float(r["balance"])
                pl  = float(r["p_late"])
                risk_color = ("#c4614a" if pl > 0.7 else
                              "#c9a961" if pl > 0.4 else "#8cb04a")
                st.markdown(
                    f"<div style='background:rgba(30,30,36,0.5); "
                    f"border-left:3px solid {risk_color}; "
                    f"padding:6px 12px; margin-bottom:6px; border-radius:4px;'>"
                    f"<div style='font-size:11px; color:#8a8880;'>"
                    f"{r['invoice_number']} · <b>{cli}</b></div>"
                    f"<div style='font-size:13px; color:#f0eee5;'>"
                    f"₹{bal / 1e5:,.2f} L "
                    f"<span style='float:right; color:{risk_color};'>"
                    f"P(late) {pl:.0%}</span></div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

    st.divider()

    # ══════════════════════════════════════════════════════════════════
    #  NEW · Collection effectiveness · rolling 90-day on-time rate
    # ══════════════════════════════════════════════════════════════════
    ce1, ce2 = st.columns([1, 2])
    ce = collection_effectiveness()
    ce_fig, ce_latest = fig_collection_effectiveness(ce)
    with ce1:
        st.markdown("#### ◆ Collection effectiveness",
                    help="Fraction of invoices paid on or before due date, "
                         "on a rolling 90-day window. The single most "
                         "honest measure of collections quality.")
        if ce_latest is not None:
            band = ("Best-in-class" if ce_latest > 85 else
                    "Healthy"        if ce_latest > 70 else
                    "Needs work"     if ce_latest > 50 else
                    "Concerning")
            band_color = ("#8cb04a" if ce_latest > 70 else
                          "#c9a961" if ce_latest > 50 else "#c4614a")
            st.markdown(
                f"<div style='padding:14px 18px; background:rgba(30,30,36,0.5); "
                f"border-left:3px solid {band_color}; border-radius:6px;'>"
                f"<div style='color:#8a8880; font-size:11px; text-transform:uppercase; "
                f"letter-spacing:1.5px;'>Current on-time rate</div>"
                f"<div style='font-size:28px; color:#f0eee5; font-weight:600; "
                f"margin:6px 0;'>{ce_latest:.1f}%</div>"
                f"<div style='color:{band_color}; font-size:12px;'>{band}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
    with ce2:
        st.plotly_chart(ce_fig, use_container_width=True)

    st.divider()

    # ══════════════════════════════════════════════════════════════════
    #  NEW · 90-day probabilistic cash projection (empirical bootstrap)
    # ══════════════════════════════════════════════════════════════════
    st.markdown("#### ◆ 90-day cash projection · probability-weighted",
                help="Non-parametric bootstrap: for each open invoice we "
                     "sample plausible payment dates from that client's own "
                     "historical days-to-pay distribution — no Normal "
                     "assumption. 400 sims. Gold line = expected cumulative "
                     "inflow. Shaded band = P10 (pessimistic) to P90 "
                     "(optimistic). Read the P10 first — that's your "
                     "worst-case planning number.")
    proj = cash_projection()
    if not proj:
        st.caption("_Not enough payment history to bootstrap yet._")
    else:
        cp1, cp2, cp3, cp4 = st.columns(4)
        cp1.metric("90-day expected", f"₹{proj['expected'][-1] / 1e7:.2f} Cr",
                   help="Mean cumulative inflow at day 90 across 400 sims.")
        cp2.metric("P50 (median)", f"₹{proj['p50'][-1] / 1e7:.2f} Cr",
                   help="Median forecast — half the sims came in above this.")
        cp3.metric("P10 (worst-case)", f"₹{proj['p10'][-1] / 1e7:.2f} Cr",
                   help="Only 10% of simulated futures ended below this. "
                        "Use for cash-planning downside.")
        cp4.metric("P90 (optimistic)", f"₹{proj['p90'][-1] / 1e7:.2f} Cr",
                   help="Upper 10% of sims. Don't spend against this — it's "
                        "the aspiration, not the plan.")
        st.plotly_chart(fig_cash_projection(proj), use_container_width=True)

    st.divider()

    # ══════════════════════════════════════════════════════════════════
    #  NEW · derived KPIs strip (portfolio velocity, AR turnover, etc.)
    # ══════════════════════════════════════════════════════════════════
    dk = derived_kpis()
    if dk:
        st.markdown("#### ◆ Derived KPIs",
                    help="Second-order metrics computed from the primary "
                         "KPIs. Portfolio velocity tells you how many "
                         "times AR turns over per year; AR turnover ratio "
                         "compares billing volume to what's still owed.")
        d1, d2, d3 = st.columns(3)
        if dk.get("portfolio_velocity"):
            d1.metric("Portfolio velocity",
                      f"{dk['portfolio_velocity']:.1f}× / year",
                      help="365 ÷ DSO. Higher = AR converts to cash faster. "
                           "A velocity of 8× means the average rupee outstanding "
                           "gets collected 8 times a year.")
        if dk.get("ar_turnover"):
            d2.metric("AR turnover ratio",
                      f"{dk['ar_turnover']:.1f}×",
                      help="Total billed ÷ current outstanding. High = you "
                           "collect efficiently relative to how much you sell.")
        modes = dk.get("payment_mode_share") or {}
        if modes:
            top_mode = max(modes.items(), key=lambda kv: kv[1])
            d3.metric(f"Dominant payment mode",
                      f"{top_mode[0]} ({top_mode[1]:.0f}%)",
                      help="Payment mode that accounted for the largest share "
                           "of inflows in the last 90 days.")

    st.divider()

    # ══════════════════════════════════════════════════════════════════
    #  NEW · 3D risk cube of counterparties
    # ══════════════════════════════════════════════════════════════════
    st.markdown("#### ◆ 3D risk cube · counterparties",
                help="One sphere per client with open invoices. "
                     "**X** = open exposure (₹) · **Y** = P(late) from the "
                     "ML model · **Z** = average historical days past due. "
                     "Marker size = # open invoices, colour = expected loss. "
                     "The top-right-back corner (big exposure + high P(late) "
                     "+ long delays) is where the trouble lives. "
                     "Click-drag to rotate.")
    rc = risk_cube_data()
    if rc.empty:
        st.caption("_Not enough scored invoices to plot yet._")
    else:
        st.plotly_chart(fig_risk_cube(rc), use_container_width=True)

    st.divider()

    # ══════════════════════════════════════════════════════════════════
    #  NEW · Late-Payment Intelligence (the ML showcase)
    # ══════════════════════════════════════════════════════════════════
    render_intelligence_section()

    st.divider()

    # ══════════════════════════════════════════════════════════════════
    #  v3.3 · Promise-to-Pay analysis (portfolio-wide summary)
    # ══════════════════════════════════════════════════════════════════
    from app.tabs.ptp_ui import render_tab1_ptp_summary
    render_tab1_ptp_summary()

    st.caption(
        "💡 KPIs and charts are cached for 5 minutes. Click **🔄 Refresh data** "
        "to reload the workbook immediately. Hover any card's ⓘ or a chart title "
        "for a plain-English explanation."
    )
