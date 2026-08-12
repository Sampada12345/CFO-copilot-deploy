import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import subprocess
import sys
import os
import time
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

# --- Timezone: app data + users are IST. Streamlit Cloud runs UTC, so set this
# before any datetime work (fixes stored created_at/sent_at timestamps; the
# due-day math in core/database.py is already IST-explicit). Linux-only no-op.
os.environ.setdefault("TZ", "Asia/Kolkata")
try:
    time.tzset()
except Exception:
    pass

# --- Path setup: put the project root on sys.path so `from core...` works
# whether you run this via `streamlit run app/main.py` or a container CMD.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# --- Load .env locally --------------------------------------------------
# On Streamlit Community Cloud, .env doesn't exist — secrets live in
# st.secrets instead.  We bridge them into os.environ so that every module
# that reads os.getenv("...") works identically in both places.
try:
    from dotenv import load_dotenv
    load_dotenv(_PROJECT_ROOT / ".env")
except ImportError:
    pass

# Streamlit Cloud → env-var bridge.  st.secrets is a Mapping that reads
# from Streamlit Cloud's Secrets panel.  Copy every key into os.environ
# so the rest of the code doesn't need a special branch.
try:
    if hasattr(st, "secrets") and len(st.secrets) > 0:
        for _k, _v in st.secrets.items():
            if isinstance(_v, (str, int, float, bool)) and _k not in os.environ:
                os.environ[_k] = str(_v)
except Exception:
    pass   # local dev without any secrets file — that's fine

# --- Restore databases from Google Drive (once per process) --------------
# Streamlit Cloud wipes local disk on restart. Before anything opens
# invoices.db / auth.db, pull the latest backup from Drive so the app starts
# with real data. Guarded to run once; never blocks/crashes startup on failure.
try:
    from services.drive_sync import restore_on_start
    restore_on_start()
except Exception as _e:
    print(f"Drive restore skipped: {_e}")

# --- Optional sklearn (LR, IsolationForest, calibration, metrics) ----
try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.ensemble import IsolationForest
    from sklearn.metrics import roc_auc_score, brier_score_loss
    SKLEARN_OK = True
except ImportError:
    SKLEARN_OK = False

# v3 modules: CSV/SQL draft source + per-client email templates
from services.csv_invoice_source import csv_source_ui
from core.template_manager import template_manager_ui

# v3.1 modules (new dashboard data source, KPI catalog, notification center)
from core.excel_source import build_view as excel_build_view
from core.excel_source import compute_kpis as excel_kpis
from core.excel_source import import_initial_load as excel_import_initial_load
from core.excel_source import XLSX_PATH as EXCEL_XLSX_PATH
from core.kpi_catalog import kpi_metric as kpi_show
from app.tabs.notification_center import render_notification_center, run_lifecycle as reminder_lifecycle
from app.tabs.executive_overview import render_tab1
from app.tabs.client_profiles import render_tab2


# ============================================================
# Page config + styles
# ============================================================
st.set_page_config(page_title="CFO Copilot | Finance & Collections", layout="wide")
from app.theme import inject_theme, render_topbar
inject_theme()
render_topbar()
# ============================================================
# ACCESS CONTROL (v3) — allowlist-restricted signup/login.
# Nothing below this block renders for anonymous visitors:
# require_login() calls st.stop() until authenticated.
# ============================================================
from app.auth import require_login, logout_button, admin_sidebar_panel

CURRENT_USER = require_login()
logout_button()
if CURRENT_USER["role"] == "admin":
    admin_sidebar_panel()

# v3.5 — start the background Gmail scheduler and trigger an on-open scan.
# Both operations are throttled/idempotent so it's safe to call every rerun.
# Failures are captured into session_state so the Tab 3 status panel can
# show them — silently swallowing them was hiding real bugs.
try:
    from services.scheduler import ensure_scheduler_and_scan
    st.session_state["_scheduler_startup"] = ensure_scheduler_and_scan()
    st.session_state["_scheduler_error"] = None
except Exception as _e:
    import traceback
    st.session_state["_scheduler_error"] = traceback.format_exc()
    st.session_state["_scheduler_startup"] = None

st.markdown("""
    <style>
    .kpi-dark {
        background-color: #161618; border-top: 3px solid #d4af37;
        padding: 14px 18px; border-radius: 6px; margin-bottom: 12px;
        min-height: 108px;
    }
    .kpi-dark-label { color: #95a5a6; font-size: 11px; text-transform: uppercase; letter-spacing: 1.2px; }
    .kpi-dark-value { color: #f4f4f5; font-size: 24px; font-weight: 700; margin: 6px 0; }
    .kpi-dark-sub   { color: #d4af37; font-size: 12px; font-weight: 500; }
    .kpi-dark-sub.warn { color: #ef4444; }
    .kpi-dark-sub.good { color: #10b981; }

    .age-card {
        background-color: #1a1a1c; border-left: 3px solid #d4af37;
        padding: 12px 16px; border-radius: 4px; margin-bottom: 8px;
    }
    .age-card-label { color: #95a5a6; font-size: 11px; text-transform: uppercase; }
    .age-card-value { color: #f4f4f5; font-size: 20px; font-weight: 600; margin: 4px 0; }
    .age-card-sub   { color: #64748b; font-size: 11px; }

    .chip {
        display: inline-block; padding: 4px 12px; border-radius: 12px;
        background-color: #2a1a1a; color: #f4a4a4; font-size: 12px;
        margin: 3px 4px 3px 0; border: 1px solid #4a2a2a;
    }
    .chip.warn { background-color: #2a2416; color: #d4af37; border-color: #4a4020; }
    .chip.good { background-color: #16281c; color: #10b981; border-color: #205030; }

    /* Chat-style comm bubbles for Tab 2 timeline */
    .msg-row { display: flex; margin: 10px 0; }
    .msg-row.out { justify-content: flex-end; }
    .msg-row.in  { justify-content: flex-start; }
    .msg-bubble {
        max-width: 72%; padding: 10px 14px; border-radius: 10px;
        font-size: 13px; line-height: 1.45;
    }
    .msg-bubble.out { background-color: #2a2416; color: #f4f4f5; border: 1px solid #4a4020; border-top-right-radius: 2px; }
    .msg-bubble.in  { background-color: #1a1a1c; color: #f4f4f5; border: 1px solid #333; border-top-left-radius: 2px; }
    .msg-meta   { font-size: 11px; color: #95a5a6; margin-bottom: 4px; }
    .msg-body   { white-space: pre-wrap; }
    .msg-sentiment { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 10px; margin-left: 6px; }

    .metric-card { background-color: #ffffff; padding: 20px; border-radius: 8px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1); border-top: 4px solid #005A8C; margin-bottom: 1rem; }
    .metric-label { font-size: 0.85rem; color: #5a7398; text-transform: uppercase; font-weight: 600; margin-bottom: 5px; }
    .metric-value { font-size: 2rem; color: #070d1a; font-weight: 700; }
    .metric-sub   { font-size: 0.8rem; color: #10b981; font-weight: 500; }
    </style>
""", unsafe_allow_html=True)



# ============================================================
# DATA LAYER — load xlsx, enrich, and produce comms_log
# ============================================================
XLSX_CANDIDATES = [
    Path(__file__).parent / "ap_ar_data.xlsx",
    Path(__file__).parent.parent / "ap_ar_data.xlsx",
    Path(__file__).parent.parent / "data" / "ap_ar_data.xlsx",
    Path("ap_ar_data.xlsx"),
]


def find_xlsx():
    for p in XLSX_CANDIDATES:
        if p.exists():
            return p
    return None


@st.cache_data(ttl=300)
def load_all_data():
    p = find_xlsx()
    if not p:
        return None

    open_items      = pd.read_excel(p, sheet_name='open_items',      parse_dates=['issue_date', 'due_date'])
    payment_history = pd.read_excel(p, sheet_name='payment_history', parse_dates=['due_date',   'paid_date'])
    counterparties  = pd.read_excel(p, sheet_name='counterparties')
    banks           = pd.read_excel(p, sheet_name='bank_accounts')
    try:
        comms_log = pd.read_excel(p, sheet_name='comms_log', parse_dates=['timestamp'])
    except Exception:
        comms_log = pd.DataFrame(columns=['invoice_id', 'timestamp', 'channel', 'direction', 'body', 'sentiment'])

    open_items      = open_items.merge(counterparties[['counterparty_id', 'name']], on='counterparty_id', how='left')
    payment_history = payment_history.merge(counterparties[['counterparty_id', 'name']], on='counterparty_id', how='left')

    # tag comms_log with counterparty via invoice_id lookup (from open_items + payment_history)
    inv_to_cp = pd.concat([
        open_items[['invoice_id', 'counterparty_id', 'name']],
        payment_history[['invoice_id', 'counterparty_id', 'name']],
    ]).drop_duplicates('invoice_id')
    comms_log = comms_log.merge(inv_to_cp, on='invoice_id', how='left')

    # ---- per-client Bayesian late estimate (fallback if LR unavailable) ----
    ar_hist = payment_history[payment_history['entity_type'] == 'AR'].copy()
    cs = ar_hist.groupby('counterparty_id').agg(
        n_history=('invoice_id', 'count'),
        late_rate=('was_late', 'mean'),
        partial_rate=('partial_flag', 'mean'),
        avg_days_to_pay=('days_to_pay', 'mean'),
        std_days_to_pay=('days_to_pay', 'std'),
    ).reset_index()
    cs['std_days_to_pay'] = cs['std_days_to_pay'].fillna(7.0)

    ALPHA, BETA = 2.0, 8.0
    cs['late_prob_bayes'] = (cs['late_rate'] * cs['n_history'] + ALPHA) / (cs['n_history'] + ALPHA + BETA)

    def _avg_days_late(g):
        late = g.loc[g['was_late'] == 1, 'days_to_pay']
        return float(late.mean() - 30) if len(late) else 0.0
    dl = ar_hist.groupby('counterparty_id', group_keys=False).apply(_avg_days_late).reset_index()
    dl.columns = ['counterparty_id', 'avg_days_late_when_late']
    cs = cs.merge(dl, on='counterparty_id', how='left')

    counterparties = counterparties.merge(cs, on='counterparty_id', how='left')
    for col, default in [
        ('late_prob_bayes', 0.20),
        ('avg_days_to_pay', 30.0), ('std_days_to_pay', 7.0),
        ('n_history', 0), ('partial_rate', 0.0), ('avg_days_late_when_late', 0.0),
        ('late_rate', 0.20),
    ]:
        counterparties[col] = counterparties[col].fillna(default)
    counterparties['n_history'] = counterparties['n_history'].astype(int)

    return {
        'open_items':      open_items,
        'payment_history': payment_history,
        'counterparties':  counterparties,
        'banks':           banks,
        'comms_log':       comms_log,
    }


data = load_all_data()


# ============================================================
# ANALYTICS LAYER
#   Everything here is derived from the raw tables.
#   Each function is small, cached, and safe to call more than once.
# ============================================================


@st.cache_resource(show_spinner=False)
def train_late_payment_model(payment_history_records):
    """
    Fit a calibrated logistic regression on `was_late` using leakage-safe
    rolling features. Trained on AR payment_history only.

    Returns dict with model, feature stats, and eval metrics — or None
    if sklearn unavailable or data is too thin.

    Note: takes `payment_history_records` as a tuple-of-tuples so
    st.cache_resource can hash it. We rebuild a DataFrame inside.
    """
    if not SKLEARN_OK:
        return None

    ph_cols = ['invoice_id', 'counterparty_id', 'entity_type',
               'amount', 'due_date', 'paid_date', 'days_to_pay',
               'was_late', 'partial_flag']
    ph = pd.DataFrame(list(payment_history_records), columns=ph_cols)
    ph['due_date']  = pd.to_datetime(ph['due_date'])
    ph['paid_date'] = pd.to_datetime(ph['paid_date'])
    ar = ph[ph['entity_type'] == 'AR'].sort_values(['counterparty_id', 'paid_date']).copy()

    # Build leakage-safe features: for each invoice we only use STRICTLY prior
    # invoices for that same client. This mimics what we'd know at scoring time.
    feats = []
    for cp_id, g in ar.groupby('counterparty_id'):
        g = g.sort_values('paid_date').reset_index(drop=True)
        for i in range(len(g)):
            past = g.iloc[:i]
            row  = g.iloc[i]
            if len(past) < 3:
                continue  # not enough history for a stable prior
            feats.append({
                'prior_late_rate':   past['was_late'].mean(),
                'prior_avg_dtp':     past['days_to_pay'].mean(),
                'prior_std_dtp':     past['days_to_pay'].std() if len(past) > 1 else 7,
                'log_amount':        np.log1p(row['amount']),
                'relative_amount':   row['amount'] / (past['amount'].median() + 1e-6),
                'tenure_n':          len(past),
                'was_late':          int(row['was_late']),
            })
    if len(feats) < 30:
        return None

    fdf = pd.DataFrame(feats)
    feature_cols = ['prior_late_rate', 'prior_avg_dtp', 'prior_std_dtp',
                    'log_amount', 'relative_amount', 'tenure_n']
    X = fdf[feature_cols].to_numpy()
    y = fdf['was_late'].to_numpy()

    scaler = StandardScaler().fit(X)
    Xs = scaler.transform(X)

    # Calibrated LR for probabilities
    base = LogisticRegression(max_iter=1000, class_weight='balanced', C=1.0)
    if len(np.unique(y)) < 2:
        return None
    try:
        cal = CalibratedClassifierCV(base, cv=3, method='sigmoid')
        cal.fit(Xs, y)
    except Exception:
        return None

    # A separate plain LR gives us clean coefficients for linear (SHAP-style) attribution
    lr = LogisticRegression(max_iter=1000, class_weight='balanced', C=1.0).fit(Xs, y)

    probs_train = cal.predict_proba(Xs)[:, 1]
    auc   = float(roc_auc_score(y, probs_train))
    brier = float(brier_score_loss(y, probs_train))

    return {
        'calibrated':      cal,
        'lr':              lr,
        'scaler':          scaler,
        'feature_cols':    feature_cols,
        'feature_means':   Xs.mean(axis=0),
        'coef':            lr.coef_[0],
        'intercept':       float(lr.intercept_[0]),
        'metrics':         {'auc': auc, 'brier': brier, 'n_train': int(len(y))},
    }


def _features_for(row_amount, past_ar_client):
    """Feature vector for scoring a new open invoice using all prior AR history for this client."""
    if len(past_ar_client) < 3:
        return None
    return {
        'prior_late_rate':  past_ar_client['was_late'].mean(),
        'prior_avg_dtp':    past_ar_client['days_to_pay'].mean(),
        'prior_std_dtp':    past_ar_client['days_to_pay'].std() if len(past_ar_client) > 1 else 7,
        'log_amount':       np.log1p(row_amount),
        'relative_amount':  row_amount / (past_ar_client['amount'].median() + 1e-6),
        'tenure_n':         len(past_ar_client),
    }


def score_open_ar(open_ar, payment_history, counterparties, model_info):
    """Attach late_prob (from calibrated LR if available, else Bayesian) plus
    per-invoice log-odds attributions for the top drivers."""
    out = open_ar.copy()

    # Fallback: Bayesian estimate at counterparty level
    fallback_prob = counterparties.set_index('counterparty_id')['late_prob_bayes'].to_dict()
    out['late_prob'] = out['counterparty_id'].map(fallback_prob).fillna(0.30)
    out['prob_source'] = 'bayes'
    out['top_drivers'] = [[] for _ in range(len(out))]

    if model_info is None:
        return out

    ar_hist = payment_history[payment_history['entity_type'] == 'AR']
    coef    = model_info['coef']
    means   = model_info['feature_means']
    scaler  = model_info['scaler']
    cal     = model_info['calibrated']
    cols    = model_info['feature_cols']

    probs, drivers_list = list(out['late_prob'].values), list(out['top_drivers'].values)
    src = list(out['prob_source'].values)

    for i, (_, r) in enumerate(out.iterrows()):
        past = ar_hist[ar_hist['counterparty_id'] == r['counterparty_id']]
        f = _features_for(r['amount'], past)
        if f is None:
            continue
        x    = np.array([[f[c] for c in cols]])
        xs   = scaler.transform(x)
        p    = float(cal.predict_proba(xs)[0, 1])
        # log-odds contribution = coef * (x_scaled - baseline)
        contribs = coef * (xs[0] - means)
        top_idx = np.argsort(-np.abs(contribs))[:3]
        drivers = [(cols[j], float(contribs[j]), float(f[cols[j]])) for j in top_idx]
        probs[i] = p
        drivers_list[i] = drivers
        src[i] = 'lr'

    out['late_prob']  = probs
    out['top_drivers'] = drivers_list
    out['prob_source'] = src
    return out


@st.cache_data(ttl=300)
def build_empirical_payment_sampler(payment_history_records):
    """For each counterparty return the empirical distribution of days_to_pay
    (a list of historical values we can bootstrap from)."""
    ph_cols = ['invoice_id', 'counterparty_id', 'entity_type',
               'amount', 'due_date', 'paid_date', 'days_to_pay',
               'was_late', 'partial_flag']
    ph = pd.DataFrame(list(payment_history_records), columns=ph_cols)
    ar = ph[ph['entity_type'] == 'AR']
    ap = ph[ph['entity_type'] == 'AP']

    ar_dist = ar.groupby('counterparty_id')['days_to_pay'].apply(list).to_dict()
    ap_dist = ap.groupby('counterparty_id')['days_to_pay'].apply(list).to_dict()
    ar_all  = ar['days_to_pay'].tolist()
    ap_all  = ap['days_to_pay'].tolist()

    return {'ar': ar_dist, 'ap': ap_dist, 'ar_all': ar_all, 'ap_all': ap_all}


def monte_carlo_cash_paths(open_ar, open_ap, banks, samplers,
                           anchor_date, n_sims=400, horizon=90, seed=42):
    """
    Simulate cash paths by bootstrapping each open invoice's payment date from
    the client's own historical days_to_pay (or the global pool if the client
    has no history).
    """
    rng = np.random.default_rng(seed)
    total_cash = banks['balance'].sum()
    dates = pd.date_range(anchor_date, periods=horizon + 1, freq='D')
    anchor_np = np.datetime64(anchor_date.date(), 'D')
    cum = np.zeros((n_sims, horizon + 1))

    ar_records = open_ar[['counterparty_id', 'issue_date', 'amount']].to_dict('records')
    ap_records = open_ap[['counterparty_id', 'issue_date', 'amount']].to_dict('records')

    ar_dist, ap_dist = samplers['ar'], samplers['ap']
    ar_all, ap_all   = samplers['ar_all'], samplers['ap_all']

    def _sample(cp_id, pool_dict, pool_all):
        pool = pool_dict.get(cp_id, pool_all)
        if not pool:
            return 30
        return int(rng.choice(pool))

    for s in range(n_sims):
        daily = np.zeros(horizon + 1)
        for r in ar_records:
            off = _sample(r['counterparty_id'], ar_dist, ar_all)
            d   = int((np.datetime64(r['issue_date'].date(), 'D')
                       + np.timedelta64(off, 'D') - anchor_np) / np.timedelta64(1, 'D'))
            if 0 <= d <= horizon:
                daily[d] += r['amount']
        for r in ap_records:
            off = _sample(r['counterparty_id'], ap_dist, ap_all)
            d   = int((np.datetime64(r['issue_date'].date(), 'D')
                       + np.timedelta64(off, 'D') - anchor_np) / np.timedelta64(1, 'D'))
            if 0 <= d <= horizon:
                daily[d] -= r['amount']
        cum[s] = total_cash + np.cumsum(daily)

    return dates, cum


def detect_anomalies(open_ar, payment_history, contamination=0.15):
    """Flag unusually-shaped open invoices with Isolation Forest.
    Features: amount, late_prob, days_to_due, amount_vs_client_median."""
    if not SKLEARN_OK or len(open_ar) < 6:
        return np.zeros(len(open_ar), dtype=bool)

    ar_hist = payment_history[payment_history['entity_type'] == 'AR']
    client_median = ar_hist.groupby('counterparty_id')['amount'].median().to_dict()

    anchor = payment_history['paid_date'].max()
    feats = []
    for _, r in open_ar.iterrows():
        med = client_median.get(r['counterparty_id'], r['amount'])
        feats.append([
            np.log1p(r['amount']),
            r['late_prob'],
            (r['due_date'] - anchor).days,
            r['amount'] / (med + 1e-6),
        ])
    X = np.array(feats)
    iso = IsolationForest(n_estimators=100, contamination=contamination,
                          random_state=42).fit(X)
    return iso.predict(X) == -1


def detect_dso_regime_shift(dso_series):
    """
    Naive binary-segmentation change-point detector: find the single split
    point that minimises within-segment SSE. Returns (change_date, mean_before,
    mean_after, magnitude) or None if the improvement is negligible.
    """
    if len(dso_series) < 20:
        return None
    y = dso_series.values.astype(float)
    n = len(y)
    total_sse = ((y - y.mean()) ** 2).sum()
    best = (None, float('inf'))
    for t in range(5, n - 5):
        left  = y[:t]
        right = y[t:]
        sse   = ((left - left.mean()) ** 2).sum() + ((right - right.mean()) ** 2).sum()
        if sse < best[1]:
            best = (t, sse)
    t_best, sse_best = best
    if t_best is None or (total_sse - sse_best) / total_sse < 0.10:
        return None  # improvement too small — no clear regime shift
    return {
        'index':       t_best,
        'change_date': dso_series.index[t_best],
        'before':      float(y[:t_best].mean()),
        'after':       float(y[t_best:].mean()),
        'magnitude':   float(y[t_best:].mean() - y[:t_best].mean()),
    }


def collection_effectiveness(payment_history, window_days=90):
    """
    Rolling on-time collection rate — proxy for CEI.
    Interpretation: of AR invoices that were paid in the last N days, what
    fraction were paid ON TIME (was_late == 0). Simple, honest, defensible.
    """
    ar = payment_history[payment_history['entity_type'] == 'AR']
    if ar.empty:
        return None
    ar = ar.sort_values('paid_date').set_index('paid_date')
    on_time = (1 - ar['was_late']).rolling(f'{window_days}D', min_periods=5).mean()
    return on_time.dropna() * 100


def client_distress_trend(client_ph):
    """Return (slope_days_per_month, direction_label) for the client's
    payment-timing trend. Positive slope = degrading. Small helper for
    the deep-dive."""
    if len(client_ph) < 6:
        return None
    df = client_ph.sort_values('paid_date').copy()
    df['months_since_start'] = (df['paid_date'] - df['paid_date'].min()).dt.days / 30.0
    if df['months_since_start'].max() < 2:
        return None
    x = df['months_since_start'].values
    y = df['days_to_pay'].values
    slope, _ = np.polyfit(x, y, 1)
    if slope > 0.5:
        return {'slope': slope, 'label': 'degrading', 'color': '#ef4444'}
    if slope < -0.5:
        return {'slope': slope, 'label': 'improving', 'color': '#10b981'}
    return {'slope': slope, 'label': 'stable', 'color': '#d4af37'}


# ============================================================
# INVOICE DB (email drafts + processed invoices) — Tab 2 timeline
# ============================================================
DB_PATH_CANDIDATES = [
    Path(__file__).parent / "invoices.db",
    Path(os.getenv("DB_PATH", "invoices.db")),
]


def find_invoices_db():
    for p in DB_PATH_CANDIDATES:
        try:
            if p.exists():
                return p
        except Exception:
            continue
    return None


def _fuzzy_match(target, candidates):
    """Very light-touch fuzzy match: case-insensitive substring both ways.
    Returns matching candidates (may be multiple)."""
    t = str(target).lower().strip()
    matches = []
    for c in candidates:
        cl = str(c).lower().strip()
        if t and cl and (t in cl or cl in t or t.split()[0] == cl.split()[0]):
            matches.append(c)
    return matches


@st.cache_data(ttl=60)
def load_agent_db_records():
    """Pull all email_drafts (with client names + invoice info) and inbound
    invoice notification metadata from invoices.db. Returns two DataFrames."""
    p = find_invoices_db()
    if not p:
        return pd.DataFrame(), pd.DataFrame()
    try:
        conn = sqlite3.connect(str(p))
        conn.row_factory = sqlite3.Row
        drafts = pd.read_sql("""
            SELECT d.id, d.to_email, d.cc_email, d.subject, d.body,
                   d.status, d.created_at, d.sent_at,
                   c.name  AS client_name,
                   i.invoice_number
              FROM email_drafts d
              JOIN clients  c ON c.id = d.client_id
              JOIN invoices i ON i.id = d.invoice_id
        """, conn)
        inbound = pd.read_sql("""
            SELECT i.invoice_number, i.email_subject, i.created_at,
                   c.name AS client_name, c.email AS client_email
              FROM invoices i
              JOIN clients  c ON c.id = i.client_id
        """, conn)
        conn.close()
        return drafts, inbound
    except Exception:
        return pd.DataFrame(), pd.DataFrame()


def build_client_timeline(counterparty_name, comms_log, db_drafts, db_inbound):
    """Merge every scrap of comm we have for this counterparty into a single
    chronological timeline, tagged by source and direction."""
    events = []

    # 1) xlsx comms_log
    if not comms_log.empty:
        cl = comms_log[comms_log['name'] == counterparty_name]
        for _, r in cl.iterrows():
            events.append({
                'when':      pd.Timestamp(r['timestamp']),
                'direction': str(r.get('direction', 'inbound')).lower(),
                'channel':   r.get('channel', 'email'),
                'source':    'comms_log',
                'subject':   f"Re: {r.get('invoice_id', '')}",
                'body':      str(r.get('body', '')),
                'sentiment': float(r['sentiment']) if pd.notna(r.get('sentiment')) else None,
                'meta':      r.get('invoice_id', ''),
            })

    # 2) invoices.db — sent/pending drafts (outbound)
    if not db_drafts.empty:
        matches = _fuzzy_match(counterparty_name, db_drafts['client_name'].unique())
        d = db_drafts[db_drafts['client_name'].isin(matches)]
        for _, r in d.iterrows():
            when_str = r['sent_at'] or r['created_at']
            try:
                when = pd.to_datetime(when_str)
            except Exception:
                continue
            events.append({
                'when':      when,
                'direction': 'outbound',
                'channel':   'email',
                'source':    f"agent · {r['status']}",
                'subject':   r['subject'] or '',
                'body':      r['body'] or '',
                'sentiment': None,
                'meta':      r.get('invoice_number', ''),
            })

    # 3) invoices.db — inbound invoice notifications processed by the agent
    if not db_inbound.empty:
        matches = _fuzzy_match(counterparty_name, db_inbound['client_name'].unique())
        d = db_inbound[db_inbound['client_name'].isin(matches)]
        for _, r in d.iterrows():
            try:
                when = pd.to_datetime(r['created_at'])
            except Exception:
                continue
            events.append({
                'when':      when,
                'direction': 'inbound',
                'channel':   'email',
                'source':    'agent · received',
                'subject':   r['email_subject'] or '',
                'body':      f"[Original invoice notification — body not stored]\nInvoice #{r.get('invoice_number', '')}",
                'sentiment': None,
                'meta':      r.get('invoice_number', ''),
            })

    events.sort(key=lambda e: e['when'])
    return events


# ============================================================
# Formatters
# ============================================================
def money(x, currency="$"):
    if x is None or (isinstance(x, float) and (np.isnan(x) or np.isinf(x))):
        return f"{currency}0"
    if abs(x) >= 1_000_000:
        return f"{currency}{x/1_000_000:.2f}M"
    if abs(x) >= 1_000:
        return f"{currency}{x/1_000:.1f}K"
    return f"{currency}{x:,.0f}"


def money_full(x, currency="$"):
    if x is None or (isinstance(x, float) and (np.isnan(x) or np.isinf(x))):
        return f"{currency}0"
    return f"{currency}{x:,.2f}"


def _sentiment_badge(s):
    if s is None or pd.isna(s):
        return ""
    if s > 0.2:
        return f'<span class="msg-sentiment" style="background:#16281c; color:#10b981;">😊 {s:+.2f}</span>'
    if s < -0.2:
        return f'<span class="msg-sentiment" style="background:#2a1a1a; color:#ef4444;">😟 {s:+.2f}</span>'
    return f'<span class="msg-sentiment" style="background:#2a2416; color:#d4af37;">😐 {s:+.2f}</span>'


# ============================================================
# Tabs
# ============================================================
tab1, tab2, tab3 = st.tabs([
    "📊 Executive Overview",
    "👤 Client Profiles",
    "🔔 Notification Center",
])


# ==============================================================================
# TAB 1 — EXECUTIVE OVERVIEW
# ==============================================================================
with tab1:
    render_tab1()

# =============================================================================
# TAB 2 — CLIENT PROFILES (rebuilt on xlsx + conversation timeline)
# ==============================================================================
with tab2:
    render_tab2()


# =============================================================================
with tab3:
    # v3.1 — Notification Center (Agent Pipeline Setup renamed & rewired)
    def _sender(to, subject, body, cc=None):
        """Real Gmail send. Errors bubble up to the UI as st.error."""
        try:
            from services.gmail_client import get_gmail_service, send_email
            svc = get_gmail_service()
            ok = send_email(svc, to=to, subject=subject, body=body, cc=cc)
            if not ok:
                st.session_state['_last_send_error'] = 'gmail_client.send_email returned False — check the console for Gmail API detail.'
            return bool(ok)
        except FileNotFoundError as e:
            st.error(
                f'Gmail credentials missing: {e}. Put credentials.json + token.json in the backend folder and restart.'
            )
            return False
        except Exception as e:
            st.error(f'Gmail send failed: {e}')
            return False
    render_notification_center(current_user_email=CURRENT_USER['email'], send_fn=_sender)
    if st.session_state.get('_last_send_error'):
        st.error(f"Last send error: {st.session_state.pop('_last_send_error')}")

    # v3.4: The manual "Scan Gmail" button has been removed.  Gmail is
    # now scanned automatically:
    #   • once when the dashboard is opened (throttled to at most every
    #     60 minutes to avoid rate limits),
    #   • plus every day at 06:00 IST via a background APScheduler job.
    # See scheduler.py.  Only messages from clients we've actually emailed
    # are considered.

    # v3.5 — visible scheduler status + last-runs table + diagnostics
    with st.expander("⚙ Gmail scan status & recent history", expanded=False):
        # Compact the three status metrics inside THIS expander only.
        # Without the div[data-testid="stExpander"] prefix, this would
        # shrink every st.metric across every tab.
        st.markdown("""
        <style>
          div[data-testid="stExpander"] div[data-testid="stMetric"] label {
              font-size: 10px !important;
              color: #8a8880 !important;
          }
          div[data-testid="stExpander"] div[data-testid="stMetricValue"] {
              font-size: 15px !important;
          }
          div[data-testid="stExpander"] div[data-testid="stMetricValue"] > div {
              font-size: 15px !important;
          }
        </style>
        """, unsafe_allow_html=True)

        # Show startup error first, if any — this is what was silently
        # hidden before and led to "🔴 idle" with no explanation.
        if st.session_state.get("_scheduler_error"):
            st.error("Scheduler failed to start:")
            st.code(st.session_state["_scheduler_error"], language="text")
            st.info(
                "Most common cause: APScheduler isn't installed. Fix in "
                "PowerShell: `pip install apscheduler` then restart Streamlit."
            )
        elif st.session_state.get("_scheduler_startup"):
            startup = st.session_state["_scheduler_startup"]
            if startup.get("daily") == "apscheduler-missing":
                st.error(
                    "APScheduler is not installed. Daily 6 AM scan is "
                    "disabled. Fix in PowerShell: "
                    "`pip install apscheduler` then restart Streamlit."
                )

        try:
            from services.scheduler import scheduler_status
            from ai.ptp_intelligence import recent_scan_history, poll_gmail_replies
            status = scheduler_status()
            hist = recent_scan_history(n=10)

            sc1, sc2, sc3 = st.columns(3)
            sc1.metric("Scheduler",
                        "🟢 running" if status["running"] else "🔴 idle")
            sc2.metric("Next daily run",
                        (status["next_run"] or "—")[:16].replace("T", " "))
            sc3.metric("Last scan",
                        (status["last_scan_at"] or "—")[:16].replace("T", " "))

            # Force-scan button — bypasses the throttle so you can test
            # right now instead of waiting for the scheduled window.
            if st.button("🔍 Run Gmail scan now (bypass throttle)",
                          use_container_width=True,
                          help="Runs the same scan the scheduler would run, "
                               "immediately. Useful for testing after fixing "
                               ".env or after new client replies arrive."):
                with st.spinner("Scanning Gmail…"):
                    result = poll_gmail_replies()
                if result.get("error"):
                    st.error(f"Scan failed: {result['error']}")
                    st.code(result["error"], language="text")
                else:
                    st.success(
                        f"Scan complete: fetched {result['fetched']}, "
                        f"processed {result['processed']} new, "
                        f"{result['ptps']} were PTPs, "
                        f"across {result['targeted_clients']} known clients."
                    )
                    if result.get("note"):
                        st.info(result["note"])
                st.rerun()

            if hist:
                import pandas as _pd
                df_hist = _pd.DataFrame(hist)
                df_hist = df_hist.rename(columns={
                    "ran_at":    "Ran at",
                    "fetched":   "Fetched",
                    "processed": "New",
                    "ptps":      "PTPs",
                    "targeted":  "Clients scanned",
                    "days_back": "Window (days)",
                    "error":     "Error",
                })
                st.dataframe(df_hist, use_container_width=True,
                              hide_index=True)
            else:
                st.caption(
                    "No scans recorded yet in v3.5. Click the button above "
                    "to run one — errors will show here."
                )
        except Exception as _e:
            st.error(f"Scheduler status unavailable: {_e}")
            import traceback
            st.code(traceback.format_exc(), language="text")


# =============================================================================
# STICKY FLOATING CHATBOT (v3.5) — must be the last thing rendered so its CSS
# lays over every tab, its dialog closes cleanly, and no downstream widget
# knocks the FAB off-screen.
# =============================================================================
try:
    from ai.chatbot import render_floating_bot
    render_floating_bot()
except Exception as _e:
    st.caption(f"_Assistant unavailable: {_e}_")
