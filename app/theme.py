"""
app/theme.py — v3.6 Theme layer for CFO Copilot.

Single source of truth for the app's visual identity. Every colour, font,
component, and Streamlit widget override lives here. Consumers call:

    from app.theme import inject_theme, render_topbar, render_kpi_strip, ...

    inject_theme()             # once at the very top of main.py
    render_topbar()            # instead of st.title + st.caption
    render_kpi_strip([...])    # instead of the vanilla st.metric grid
    render_aging_svg(agg_df)   # instead of the plotly aging bars
    render_dso_svg(trend_df)   # instead of the plotly DSO line
    render_action_queue(items) # replaces the inline HTML in exec_overview
    render_panel_header(title) # gold-diamond section header
    render_chip(text, tone)    # dark-palette chips for status/countdown

The tokens (`--gold`, `--ink`, `--surface`, etc.) match cfo_copilot_ui_preview.html
1:1, so anything you drop straight-HTML into the app inherits automatically.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Sequence

import pandas as pd
import streamlit as st

# ═════════════════════════════════════════════════════════════════════════════
#  PALETTE — mirror the HTML preview 1:1.  If you're tempted to reach for a
#  hex code inside a component below, put it here first and reference the
#  --var name.  One place to change the whole app.
# ═════════════════════════════════════════════════════════════════════════════
INK       = "#f0eee5"
INK_DIM   = "#c9c4b6"
MUTED     = "#8a8880"
FAINT     = "#6b6a63"
BG        = "#0a0a0d"
SURFACE   = "#111114"
SURFACE_2 = "#16161b"
BORDER    = "#23232a"
BORDER_2  = "#2f2f38"
GOLD      = "#c9a961"
GOLD_HI   = "#d4af37"
GOOD      = "#8cb04a"
BAD       = "#c4614a"

# Aging bucket colours (Current → 90+) — the mockup's gradient.
AGING_COLORS = ["#146c43", "#b8860b", "#d97706", "#dc6b2f", "#c0392b"]


# ═════════════════════════════════════════════════════════════════════════════
#  inject_theme() — the one CSS block.  Idempotent; safe to call every rerun.
# ═════════════════════════════════════════════════════════════════════════════
_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

:root {{
  --ink: {INK}; --ink-dim: {INK_DIM}; --muted: {MUTED}; --faint: {FAINT};
  --bg: {BG}; --surface: {SURFACE}; --surface-2: {SURFACE_2};
  --border: {BORDER}; --border-2: {BORDER_2};
  --gold: {GOLD}; --gold-hi: {GOLD_HI};
  --good: {GOOD}; --bad: {BAD};
}}

/* --- Streamlit shell: dark background, hide default chrome ------------- */
.stApp {{
  background: radial-gradient(1100px 520px at 50% -12%, #16161d 0%, transparent 62%), {BG};
  color: {INK};
  font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
}}
header[data-testid="stHeader"] {{ background: transparent; }}
#MainMenu, footer {{ visibility: hidden; }}

/* Tighten Streamlit's default top padding so our topbar sits close to the edge */
.block-container {{ padding-top: 1.5rem; padding-bottom: 2rem; max-width: 1250px; }}

/* --- Typography defaults ---------------------------------------------- */
.stApp, .stMarkdown, .stCaption, p, span, div {{ color: {INK}; }}
.stCaption, [data-testid="stCaptionContainer"] {{ color: {FAINT} !important; }}

h1, h2, h3, h4, h5 {{ color: {INK}; font-weight: 600; letter-spacing: .2px; }}
code, pre, kbd, samp {{ font-family: 'JetBrains Mono', 'Consolas', monospace; }}

/* --- Streamlit widgets: tabs (mockup: gold underline on active) ------- */
.stTabs [data-baseweb="tab-list"] {{
  gap: 30px;
  border-bottom: 1px solid {BORDER};
  background: transparent;
}}
.stTabs [data-baseweb="tab"] {{
  height: auto;
  padding: 10px 0;
  background: transparent !important;
  border: none !important;
  border-radius: 0;
  color: {MUTED};
  font-size: 13.5px;
  font-weight: 500;
  letter-spacing: .4px;
}}
.stTabs [aria-selected="true"] {{
  color: {INK} !important;
  font-weight: 600;
  border-bottom: 2px solid {GOLD} !important;
}}
.stTabs [data-baseweb="tab-highlight"] {{ display: none; }}

/* --- Streamlit metric — restyle to match the KPI cards --------------- */
[data-testid="stMetric"] {{
  background: {SURFACE};
  border: 1px solid {BORDER};
  border-radius: 12px;
  padding: 15px 17px 13px;
  position: relative;
  overflow: hidden;
  transition: .18s;
}}
[data-testid="stMetric"]::before {{
  content: "";
  position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, {GOLD}, transparent 72%);
  opacity: .45; transition: .18s;
}}
[data-testid="stMetric"]:hover {{
  border-color: {BORDER_2};
  transform: translateY(-1px);
}}
[data-testid="stMetric"]:hover::before {{ opacity: .95; }}

[data-testid="stMetricLabel"] p,
[data-testid="stMetricLabel"] {{
  color: {MUTED} !important;
  font-size: 10.5px !important;
  font-weight: 600;
  letter-spacing: 1.4px;
  text-transform: uppercase;
}}
[data-testid="stMetricValue"] {{
  color: {INK} !important;
  font-family: 'JetBrains Mono', monospace !important;
  font-size: 1.62rem !important;
  font-weight: 600;
  letter-spacing: -.5px;
  margin: 7px 0 3px !important;
}}
[data-testid="stMetricValue"] > div {{
  font-family: 'JetBrains Mono', monospace !important;
  font-size: 1.62rem !important;
}}
[data-testid="stMetricDelta"] {{
  color: {GOLD} !important;
  font-size: .78rem !important;
  font-weight: 500;
}}

/* --- Buttons: subtle dark surface, gold on hover --------------------- */
.stButton > button {{
  background: {SURFACE};
  color: {INK};
  border: 1px solid {BORDER};
  border-radius: 8px;
  font-weight: 500;
  transition: .15s;
}}
.stButton > button:hover {{
  border-color: {GOLD};
  color: {GOLD};
}}

/* --- Inputs, selects, textareas -------------------------------------- */
.stSelectbox > div > div,
.stTextInput > div > div > input,
.stTextArea textarea,
.stMultiSelect > div > div,
.stDateInput > div > div > input {{
  background: {SURFACE} !important;
  border-color: {BORDER} !important;
  color: {INK} !important;
}}

/* --- Dividers ------------------------------------------------------- */
hr, [data-testid="stDivider"] {{
  border-color: {BORDER} !important;
  background-color: {BORDER} !important;
}}

/* --- DataFrame: dark surface --------------------------------------- */
[data-testid="stDataFrame"] {{
  background: {SURFACE};
  border: 1px solid {BORDER};
  border-radius: 8px;
}}

/* ═══════════════════════════════════════════════════════════════════
   Custom components (all rendered via st.markdown from Python)
   ═══════════════════════════════════════════════════════════════════ */

/* --- Topbar --------------------------------------------------------- */
.cfo-topbar {{
  display: flex; align-items: flex-end; justify-content: space-between;
  gap: 24px;
  padding: 4px 2px 16px;
  border-bottom: 1px solid {BORDER};
  margin-bottom: 22px;
}}
.cfo-brand {{ display: flex; align-items: center; gap: 16px; }}
.cfo-brand-mark {{
  font-size: 22px; font-weight: 700; letter-spacing: .5px; line-height: 1;
  color: {INK};
}}
.cfo-brand-mark span {{
  font-weight: 300; letter-spacing: 3px; color: {GOLD}; margin-left: 7px;
}}
.cfo-brand-rule {{ width: 1px; height: 26px; background: {BORDER_2}; }}
.cfo-brand-tag {{
  color: {MUTED}; font-size: 11px; letter-spacing: 2.4px;
  text-transform: uppercase;
}}

/* --- KPI cards ------------------------------------------------------- */
.cfo-kpis {{
  display: grid; grid-template-columns: repeat(6, 1fr); gap: 14px;
  margin-bottom: 26px;
}}
@media (max-width: 900px) {{ .cfo-kpis {{ grid-template-columns: repeat(2, 1fr); }} }}
.cfo-kpi {{
  background: {SURFACE};
  border: 1px solid {BORDER};
  border-radius: 12px;
  padding: 15px 17px 13px;
  position: relative;
  overflow: hidden;
  transition: .18s;
}}
.cfo-kpi::before {{
  content: "";
  position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, {GOLD}, transparent 72%);
  opacity: .45; transition: .18s;
}}
.cfo-kpi:hover {{ border-color: {BORDER_2}; transform: translateY(-1px); }}
.cfo-kpi:hover::before {{ opacity: .95; }}
.cfo-kpi .lbl {{
  color: {MUTED}; font-size: 10.5px; font-weight: 600;
  letter-spacing: 1.4px; text-transform: uppercase;
}}
.cfo-kpi .val {{
  color: {INK}; font-family: 'JetBrains Mono', monospace;
  font-size: 1.62rem; font-weight: 600; letter-spacing: -.5px;
  margin: 7px 0 3px;
}}
.cfo-kpi .sub {{ font-size: .78rem; font-weight: 500; }}
.cfo-kpi .sub.up  {{ color: {GOOD}; }}
.cfo-kpi .sub.dn  {{ color: {BAD}; }}
.cfo-kpi .sub.neu {{ color: {GOLD}; }}

/* --- Panel + panel header ------------------------------------------ */
.cfo-panel {{
  background: {SURFACE};
  border: 1px solid {BORDER};
  border-radius: 12px;
  padding: 18px 20px;
  margin-bottom: 20px;
}}
.cfo-panel-h {{
  color: {INK}; font-weight: 600; font-size: 1.02rem;
  letter-spacing: .3px; margin: 0 0 12px;
}}
.cfo-panel-h .d {{ color: {GOLD}; margin-right: 7px; }}

/* --- Aging bar chart ----------------------------------------------- */
.cfo-bars {{
  display: flex; align-items: flex-end; gap: 22px;
  height: 210px; padding: 6px 6px 0;
}}
.cfo-bar-col {{
  flex: 1; display: flex; flex-direction: column; align-items: center;
  gap: 8px; height: 100%;
}}
.cfo-bar-track {{ flex: 1; width: 100%; display: flex; align-items: flex-end; }}
.cfo-bar {{ width: 100%; border-radius: 5px 5px 0 0; min-height: 3px; }}
.cfo-bar-amt {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px; color: {INK_DIM};
}}
.cfo-bar-lbl {{ font-size: 11px; color: {MUTED}; letter-spacing: .4px; }}

/* --- Action queue --------------------------------------------------- */
.cfo-aq {{ display: flex; flex-direction: column; gap: 8px; }}
.cfo-aq-item {{
  background: rgba(30, 30, 36, .5);
  border-left: 3px solid {GOLD};
  padding: 8px 13px; border-radius: 5px;
}}
.cfo-aq-item.hi {{ border-left-color: {BAD}; }}
.cfo-aq-item.md {{ border-left-color: {GOLD}; }}
.cfo-aq-item.lo {{ border-left-color: {GOOD}; }}
.cfo-aq-top {{ font-size: 11px; color: {MUTED}; }}
.cfo-aq-bot {{
  font-size: 13px; color: {INK};
  font-family: 'JetBrains Mono', monospace;
  display: flex; justify-content: space-between; margin-top: 2px;
}}
.cfo-aq-p.hi {{ color: {BAD}; }}
.cfo-aq-p.md {{ color: {GOLD}; }}
.cfo-aq-p.lo {{ color: {GOOD}; }}

/* --- Chips (dark palette, replaces the light chips) ---------------- */
.cfo-chip {{
  display: inline-block;
  padding: 3px 11px; border-radius: 999px;
  font-size: 11.5px; font-weight: 600; letter-spacing: .3px;
  border: 1px solid transparent;
  margin: 2px 4px 2px 0;
}}
.cfo-chip-today  {{ background: rgba(196,97,74,.16); color: #e39a83; border-color: rgba(196,97,74,.32); }}
.cfo-chip-soon   {{ background: rgba(201,169,97,.15); color: #d9bd7e; border-color: rgba(201,169,97,.30); }}
.cfo-chip-later  {{ background: rgba(255,255,255,.05); color: #b8b6ad; border-color: rgba(255,255,255,.10); }}
.cfo-chip-await  {{ background: rgba(201,169,97,.15); color: #d9bd7e; border-color: rgba(201,169,97,.30); }}
.cfo-chip-review {{ background: rgba(140,176,74,.15); color: #a8c46e; border-color: rgba(140,176,74,.30); }}

/* --- DSO / line SVG containers ------------------------------------ */
.cfo-line-svg {{ width: 100%; height: 210px; display: block; }}
</style>
"""


def inject_theme() -> None:
    """Inject the CSS variables + component styles.  Idempotent.

    Call this exactly once, immediately after st.set_page_config() at the
    top of app/main.py.  Every helper below assumes this has run.
    """
    st.markdown(_CSS, unsafe_allow_html=True)


# ═════════════════════════════════════════════════════════════════════════════
#  render_topbar() — brand mark + tagline.  No SAP pill, no live indicator.
# ═════════════════════════════════════════════════════════════════════════════
def render_topbar(
    brand: str = "CFO",
    brand_accent: str = "COPILOT",
    tagline: str = "Accounts Receivable Intelligence",
) -> None:
    """Render the app-wide topbar (replaces st.title + st.caption).

    Matches cfo_copilot_ui_preview.html: gold-accent brand mark, vertical rule,
    uppercase spaced tag.  The right side of the mockup ("SAP connected · live")
    is intentionally omitted per the current spec.
    """
    st.markdown(
        f"""
        <div class="cfo-topbar">
          <div class="cfo-brand">
            <div class="cfo-brand-mark">{brand}<span>{brand_accent}</span></div>
            <div class="cfo-brand-rule"></div>
            <div class="cfo-brand-tag">{tagline}</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  KPI strip
# ═════════════════════════════════════════════════════════════════════════════
def render_kpi_strip(items: Sequence[Mapping[str, str]]) -> None:
    """Render a horizontal grid of KPI cards.

    items: list of dicts, each with:
      - label:  small-caps header ("Total Outstanding")
      - value:  the big number ("₹4.82 Cr")
      - sub:    optional sub-text ("312 open invoices")
      - tone:   "up" (green), "dn" (red), "neu" (gold).  Default "neu".

    Renders as one <div> so the grid math lands right — using st.columns
    would introduce inter-column padding that fights the mockup layout.
    """
    cards_html = []
    for it in items:
        tone = (it.get("tone") or "neu").lower()
        if tone not in ("up", "dn", "neu"):
            tone = "neu"
        sub = it.get("sub") or ""
        sub_html = f"<div class='sub {tone}'>{sub}</div>" if sub else ""
        cards_html.append(
            f"""
            <div class="cfo-kpi">
              <div class="lbl">{it.get('label','')}</div>
              <div class="val">{it.get('value','—')}</div>
              {sub_html}
            </div>
            """
        )
    st.markdown(
        f"<div class='cfo-kpis'>{''.join(cards_html)}</div>",
        unsafe_allow_html=True,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Panel header ("◆ Section title")
# ═════════════════════════════════════════════════════════════════════════════
def render_panel_header(title: str) -> None:
    """Gold-diamond section header.  Sits above panels / charts."""
    st.markdown(
        f"<h4 class='cfo-panel-h'><span class='d'>◆</span>{title}</h4>",
        unsafe_allow_html=True,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Aging bar chart — pure CSS, exact mockup match, real data
# ═════════════════════════════════════════════════════════════════════════════
def render_aging_svg(
    agg: pd.DataFrame,
    money_fn=None,
) -> None:
    """Render the AR-aging bar chart from the aggregated bucket DataFrame.

    Expects columns: bucket ('Current' | '1–30' | '31–60' | '61–90' | '90+'),
    amount (float), count (int).  If `money_fn` is not passed, uses a simple
    Cr/L formatter.

    All 5 buckets are always shown, even if some are zero — the mockup expects
    a fixed 5-column layout.
    """
    if money_fn is None:
        def money_fn(v):
            v = float(v or 0)
            if abs(v) >= 1e7: return f"₹{v/1e7:,.2f} Cr"
            if abs(v) >= 1e5: return f"₹{v/1e5:,.2f} L"
            return f"₹{v:,.0f}"

    order = ["Current", "1–30", "31–60", "61–90", "90+"]
    if agg is None or agg.empty:
        rows = [{"bucket": b, "amount": 0.0, "count": 0} for b in order]
    else:
        m = {r["bucket"]: r for _, r in agg.iterrows()}
        rows = []
        for b in order:
            if b in m:
                rows.append({
                    "bucket": b,
                    "amount": float(m[b].get("amount", 0) or 0),
                    "count":  int(m[b].get("count", 0) or 0),
                })
            else:
                rows.append({"bucket": b, "amount": 0.0, "count": 0})

    max_amt = max((r["amount"] for r in rows), default=0) or 1.0
    cols_html = []
    for r, colour in zip(rows, AGING_COLORS):
        h_pct = max(3, int(100 * r["amount"] / max_amt)) if r["amount"] > 0 else 3
        cols_html.append(f"""
        <div class="cfo-bar-col">
          <span class="cfo-bar-amt">{money_fn(r['amount'])}</span>
          <div class="cfo-bar-track">
            <div class="cfo-bar" style="height:{h_pct}%; background:{colour};"></div>
          </div>
          <span class="cfo-bar-lbl">{r['bucket']}</span>
        </div>
        """)

    st.markdown(
        f"<div class='cfo-bars'>{''.join(cols_html)}</div>",
        unsafe_allow_html=True,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  DSO trend SVG — gold line + soft gradient, real data
# ═════════════════════════════════════════════════════════════════════════════
def render_dso_svg(trend: pd.DataFrame, dso_col: str = "dso") -> None:
    """Render the DSO trend line SVG.

    Expects a DataFrame with a numeric column (default 'dso').  Empty /
    single-point inputs render as an empty axis so the panel doesn't collapse.
    """
    if trend is None or trend.empty or dso_col not in trend.columns:
        st.markdown(
            f"<div class='cfo-line-svg' style='display:flex;align-items:center;"
            f"justify-content:center;color:{FAINT};font-size:12px;'>"
            f"Not enough data to plot the trend.</div>",
            unsafe_allow_html=True,
        )
        return

    ys = trend[dso_col].dropna().astype(float).tolist()
    if len(ys) < 2:
        st.markdown(
            f"<div class='cfo-line-svg' style='display:flex;align-items:center;"
            f"justify-content:center;color:{FAINT};font-size:12px;'>"
            f"Not enough closed invoices yet to draw a trend.</div>",
            unsafe_allow_html=True,
        )
        return

    # Map values to viewBox coordinates.  We hard-code viewBox to 640×200 so
    # the SVG scales fluidly with the panel width.
    vw, vh = 640, 200
    pad_top, pad_bot = 20, 20
    y_min = min(ys)
    y_max = max(ys)
    y_range = y_max - y_min if y_max > y_min else 1.0

    def to_xy(i, y):
        x = i / (len(ys) - 1) * vw
        y_norm = (y - y_min) / y_range         # 0=low, 1=high
        y_pix = vh - pad_bot - y_norm * (vh - pad_top - pad_bot)
        return x, y_pix

    points = [to_xy(i, y) for i, y in enumerate(ys)]
    line_d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    area_d = line_d + f" L {points[-1][0]:.1f},{vh} L {points[0][0]:.1f},{vh} Z"

    # 3 subtle grid lines (25% / 50% / 75%)
    grid = "".join(
        f'<line x1="0" y1="{int(vh * f)}" x2="{vw}" y2="{int(vh * f)}" '
        f'stroke="rgba(255,255,255,.05)"/>'
        for f in (0.25, 0.5, 0.75)
    )

    svg = f"""
    <svg class="cfo-line-svg" viewBox="0 0 {vw} {vh}" preserveAspectRatio="none">
      <defs>
        <linearGradient id="cfoDsoGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="{GOLD}" stop-opacity=".22"/>
          <stop offset="1" stop-color="{GOLD}" stop-opacity="0"/>
        </linearGradient>
      </defs>
      {grid}
      <path d="{area_d}" fill="url(#cfoDsoGrad)"/>
      <path d="{line_d}" fill="none" stroke="{GOLD}" stroke-width="2.5"
            stroke-linejoin="round" stroke-linecap="round"/>
    </svg>
    """
    st.markdown(svg, unsafe_allow_html=True)


# ═════════════════════════════════════════════════════════════════════════════
#  Action queue — priority-colored left borders
# ═════════════════════════════════════════════════════════════════════════════
def render_action_queue(items: Iterable[Mapping]) -> None:
    """Render the ranked list of invoices needing action.

    items: iterable of dicts with:
      - invoice: invoice number ("ITPL/24-25/0412")
      - client:  client name (bold in the top row)
      - amount:  pre-formatted amount string ("₹18.40 L")
      - p_late:  float 0..1  (drives colour + the "P(late) 81%" label)
    """
    rows = []
    for it in items:
        p = float(it.get("p_late", 0) or 0)
        if   p >= 0.70: tone = "hi"
        elif p >= 0.40: tone = "md"
        else:           tone = "lo"
        rows.append(f"""
        <div class="cfo-aq-item {tone}">
          <div class="cfo-aq-top">{it.get('invoice','—')} · <b>{it.get('client','—')}</b></div>
          <div class="cfo-aq-bot">
            <span>{it.get('amount','—')}</span>
            <span class="cfo-aq-p {tone}">P(late) {p:.0%}</span>
          </div>
        </div>
        """)
    if not rows:
        rows.append(
            f"<div style='color:{FAINT};font-size:12px;padding:8px 4px;'>"
            f"No invoices to prioritise right now.</div>"
        )
    st.markdown(
        f"<div class='cfo-aq'>{''.join(rows)}</div>",
        unsafe_allow_html=True,
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Chips — dark palette equivalents of the old light chips
# ═════════════════════════════════════════════════════════════════════════════
_CHIP_TONES = {"today", "soon", "later", "await", "review"}


def chip_html(text: str, tone: str = "later") -> str:
    """Return the raw HTML for a chip — use inside another st.markdown call
    when embedding a chip inside a larger rendered block."""
    tone = tone if tone in _CHIP_TONES else "later"
    return f"<span class='cfo-chip cfo-chip-{tone}'>{text}</span>"


def render_chip(text: str, tone: str = "later") -> None:
    """Render a single chip as its own line — thin wrapper over chip_html."""
    st.markdown(chip_html(text, tone), unsafe_allow_html=True)


# ═════════════════════════════════════════════════════════════════════════════
#  Panel wrapper — for consistent card containers.  Use as a context manager:
#
#    with panel("DSO trend"):
#        render_dso_svg(trend_df)
# ═════════════════════════════════════════════════════════════════════════════
from contextlib import contextmanager


@contextmanager
def panel(title: str | None = None):
    """Context manager: opens a styled panel div, renders the optional
    header, yields for content, closes the div.

    Only works cleanly when the body uses st.markdown / native widgets —
    Streamlit doesn't let arbitrary HTML wrap around widgets, so the panel
    background is a single-piece block above/around, not a true wrapper.
    In practice: use render_panel_header() + native content when you need
    Streamlit widgets inside; use `with panel()` when the content is all
    HTML (chips, action queue, aging bars).
    """
    st.markdown("<div class='cfo-panel'>", unsafe_allow_html=True)
    if title:
        render_panel_header(title)
    try:
        yield
    finally:
        st.markdown("</div>", unsafe_allow_html=True)
