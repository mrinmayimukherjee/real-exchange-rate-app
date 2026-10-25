"""Real exchange rate explorer.

Pulls monthly CPI and exchange-rate data from the IMF SDMX 3.0 API and
computes the real exchange rate (RER) between two economies.

Conventions
    E(t)  IMF exchange rate, local currency per USD (USD = 1 by definition)
    S(t)  units of Country 2 currency per 1 unit of Country 1 currency = E2 / E1
    RER   S(t) * P1(t) / P2(t)
    RER*  average RER over valid months in the selected sample
    Index 100 * RER(t) / RER*
"""

from __future__ import annotations

import html
import io
import math
import time

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

try:
    import pycountry
except Exception:  # library missing: fall back to a short built-in list
    pycountry = None

try:
    from babel import Locale
    from babel.numbers import get_currency_name, get_territory_currencies
except Exception:  # library missing: fall back to a short built-in list
    Locale = None
    get_currency_name = None
    get_territory_currencies = None


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

START = pd.Timestamp("1976-01-01")
START_PERIOD = "1976-M01"
API_URL = "https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.STA/{flow}/~/{key}"
ACCEPT_HEADERS = ("text/csv", "application/vnd.sdmx.data+csv;version=2.0.0")
USER_AGENT = "rer-explorer/1.0 (Streamlit Community Cloud; educational use)"
TIMEOUT = (8, 30)  # (connect, read) seconds
RETRIES = 3
EURO_AREA = "G163"
PRESETS = ["Full history", "Since 2000", "Last 10 years", "Custom"]
VIEWS = ["Real", "Nominal", "Both"]
FONT_STACK = "Public Sans, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"

PALETTES = {
    "light": {
        "ink": "#14213D",
        "muted": "#6B7280",
        "teal": "#0F766E",
        "amber": "#B45309",
        "above": "rgba(15, 118, 110, 0.14)",
        "below": "rgba(107, 114, 128, 0.16)",
        "grid": "rgba(20, 33, 61, 0.08)",
        "button": "rgba(20, 33, 61, 0.06)",
        "button_active": "rgba(15, 118, 110, 0.20)",
    },
    "dark": {
        "ink": "#E8ECF4",
        "muted": "#9CA3AF",
        "teal": "#2DD4BF",
        "amber": "#F59E0B",
        "above": "rgba(45, 212, 191, 0.16)",
        "below": "rgba(156, 163, 175, 0.16)",
        "grid": "rgba(255, 255, 255, 0.08)",
        "button": "rgba(255, 255, 255, 0.06)",
        "button_active": "rgba(45, 212, 191, 0.24)",
    },
}

NAME_OVERRIDES = {
    "HK": "Hong Kong SAR",
    "MO": "Macao SAR",
    "CD": "Congo, Dem. Rep.",
    "CG": "Congo, Rep.",
    "MM": "Myanmar",
    "PS": "West Bank and Gaza",
}

FALLBACK_COUNTRIES = {
    "USA": ("United States", "USD", "US dollar"),
    "GBR": ("United Kingdom", "GBP", "British pound"),
    "JPN": ("Japan", "JPY", "Japanese yen"),
    "CAN": ("Canada", "CAD", "Canadian dollar"),
    "CHE": ("Switzerland", "CHF", "Swiss franc"),
    "DEU": ("Germany", "EUR", "euro"),
    "FRA": ("France", "EUR", "euro"),
    "ITA": ("Italy", "EUR", "euro"),
    "ESP": ("Spain", "EUR", "euro"),
    "CHN": ("China", "CNY", "Chinese yuan"),
    "IND": ("India", "INR", "Indian rupee"),
    "BRA": ("Brazil", "BRL", "Brazilian real"),
    "MEX": ("Mexico", "MXN", "Mexican peso"),
    "KOR": ("South Korea", "KRW", "South Korean won"),
    "AUS": ("Australia", "AUD", "Australian dollar"),
    "NZL": ("New Zealand", "NZD", "New Zealand dollar"),
    "ZAF": ("South Africa", "ZAR", "South African rand"),
    "TUR": ("Turkey", "TRY", "Turkish lira"),
    "SWE": ("Sweden", "SEK", "Swedish krona"),
    "NOR": ("Norway", "NOK", "Norwegian krone"),
    "DNK": ("Denmark", "DKK", "Danish krone"),
}


class IMFUnavailable(Exception):
    """Raised when the IMF API cannot be reached after retries (never cached)."""


# --------------------------------------------------------------------------
# Countries and currencies
# --------------------------------------------------------------------------

def _sentence_currency(name: str) -> str:
    """'British Pound' -> 'British pound', 'Euro' -> 'euro', 'US Dollar' -> 'US dollar'."""
    words = name.split()
    if words and words[-1][:1].isupper() and not words[-1].isupper():
        words[-1] = words[-1].lower()
    return " ".join(words)


def _currency_for(alpha2: str):
    if get_territory_currencies is None or get_currency_name is None:
        return None, None
    try:
        codes = get_territory_currencies(alpha2)
        if codes:
            code = codes[0]
            return code, _sentence_currency(get_currency_name(code, locale="en"))
    except Exception:
        pass
    return None, None


@st.cache_data(show_spinner=False)
def country_catalog() -> dict:
    """ISO3 code -> {name, currency, currency_name}, sorted by name."""
    catalog: dict = {}
    if pycountry is not None and Locale is not None:
        try:
            territories = Locale.parse("en").territories
        except Exception:
            territories = {}
        for c in pycountry.countries:
            a2, a3 = c.alpha_2, c.alpha_3
            name = (
                NAME_OVERRIDES.get(a2)
                or territories.get(a2)
                or getattr(c, "common_name", None)
                or c.name
            )
            code, cname = _currency_for(a2)
            catalog[a3] = {
                "name": name,
                "currency": code or a3,
                "currency_name": cname or f"{name} currency",
            }
        catalog["XKX"] = {"name": "Kosovo", "currency": "EUR", "currency_name": "euro"}
    else:
        for a3, (name, code, cname) in FALLBACK_COUNTRIES.items():
            catalog[a3] = {"name": name, "currency": code, "currency_name": cname}

    catalog[EURO_AREA] = {"name": "Euro area", "currency": "EUR", "currency_name": "euro"}
    catalog.setdefault("USA", {"name": "United States", "currency": "USD", "currency_name": "US dollar"})
    catalog["USA"].update({"currency": "USD", "currency_name": "US dollar"})
    catalog.setdefault("GBR", {"name": "United Kingdom", "currency": "GBP", "currency_name": "British pound"})
    return dict(sorted(catalog.items(), key=lambda kv: kv[1]["name"]))


# --------------------------------------------------------------------------
# IMF data access
# --------------------------------------------------------------------------

def _parse_csv(text: str | None):
    """Return a monthly Series (month-start index) or None if the text is not a usable CSV."""
    if text is None:
        return None
    if not text.strip():
        return pd.Series(dtype=float)
    try:
        df = pd.read_csv(io.StringIO(text), dtype=str)
    except Exception:
        return None
    cols = {str(c).strip().upper(): c for c in df.columns}
    if "TIME_PERIOD" not in cols or "OBS_VALUE" not in cols:
        return None
    if df.empty:
        return pd.Series(dtype=float)

    periods = df[cols["TIME_PERIOD"]].astype(str).str.strip()
    parts = periods.str.extract(r"^(\d{4})-?M?(\d{1,2})$")
    dates = pd.to_datetime(
        parts[0] + "-" + parts[1].str.zfill(2) + "-01", errors="coerce", format="%Y-%m-%d"
    )
    values = pd.to_numeric(df[cols["OBS_VALUE"]], errors="coerce")
    s = pd.Series(values.to_numpy(), index=dates)
    s = s[s.index.notna()].dropna()
    if s.empty:
        return pd.Series(dtype=float)
    s = s.groupby(level=0).last().sort_index()
    s = s[s.index >= START]  # local filter in case the server ignored ours
    s.index.name = None
    return s.astype(float)


def _request(url: str, params: dict | None, accept: str):
    """One logical request with retries. Returns (status, payload)."""
    headers = {"Accept": accept, "User-Agent": USER_AGENT}
    last_error = "unknown error"
    for attempt in range(RETRIES):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        except requests.Timeout:
            last_error = "the request timed out"
        except requests.RequestException as exc:
            last_error = f"network error ({exc.__class__.__name__})"
        else:
            if r.status_code == 200:
                return "ok", r.text
            if r.status_code in (204, 404):
                return "empty", None
            if r.status_code in (429, 500, 502, 503, 504):
                last_error = f"HTTP {r.status_code} from api.imf.org"
            else:
                return "bad_request", None  # 400, 406, 413, 422 ...: try a different request shape
        if attempt < RETRIES - 1:
            time.sleep(1.5 * (attempt + 1))
    raise IMFUnavailable(last_error)


@st.cache_data(ttl=6 * 3600, show_spinner=False, max_entries=512)
def _fetch_cached(flow: str, key: str) -> pd.Series:
    url = API_URL.format(flow=flow, key=key)
    for accept in ACCEPT_HEADERS:
        for params in ({"c[TIME_PERIOD]": f"ge:{START_PERIOD}"}, None):
            status, payload = _request(url, params, accept)
            if status == "ok":
                s = _parse_csv(payload)
                if s is not None and not s.empty:
                    return s
            elif status == "empty" and params is None:
                return pd.Series(dtype=float)  # unfiltered 404: the series does not exist
    return pd.Series(dtype=float)


def fetch_series(flow: str, key: str):
    """Returns (series, error_message). Network failures are not cached."""
    try:
        return _fetch_cached(flow, key), None
    except IMFUnavailable as exc:
        return pd.Series(dtype=float), str(exc)
    except Exception as exc:
        return pd.Series(dtype=float), f"unexpected error ({exc.__class__.__name__})"


def load_country(iso3: str) -> dict:
    info = {
        "iso3": iso3,
        "cpi": None,
        "cpi_key": None,
        "cpi_tried": [],
        "er": None,
        "er_key": f"{iso3}.XDC_USD.PA_RT.M",
        "er_fixed": iso3 == "USA",
        "error": None,
    }
    for key in (f"{iso3}.CPI._T.IX.M", f"{iso3}.HICP._T.IX.M"):
        info["cpi_tried"].append(key)
        s, err = fetch_series("CPI", key)
        if err:
            info["error"] = f"the CPI request ({key}) failed: {err}"
            return info
        if s.notna().any():
            info["cpi"], info["cpi_key"] = s, key
            break
    if not info["er_fixed"]:
        s, err = fetch_series("ER", info["er_key"])
        if err:
            info["error"] = f"the exchange-rate request ({info['er_key']}) failed: {err}"
            return info
        if s.notna().any():
            info["er"] = s
    return info


def build_frame(d1: dict, d2: dict) -> pd.DataFrame | None:
    series = [d1["cpi"], d2["cpi"]] + [d["er"] for d in (d1, d2) if not d["er_fixed"]]
    indexes = [s.dropna().index for s in series if s is not None and s.notna().any()]
    if not indexes:
        return None
    lo = max(START, min(i.min() for i in indexes))
    hi = max(i.max() for i in indexes)
    months = pd.date_range(lo, hi, freq="MS")

    def er(d):
        if d["er_fixed"]:
            return pd.Series(1.0, index=months)
        return d["er"].reindex(months)

    df = pd.DataFrame(
        {
            "E1": er(d1),
            "E2": er(d2),
            "P1": d1["cpi"].reindex(months),
            "P2": d2["cpi"].reindex(months),
        },
        index=months,
    )
    pos = df[["E1", "E2", "P1", "P2"]].gt(0)  # NaN compares False
    df["S"] = (df["E2"] / df["E1"]).where(pos["E1"] & pos["E2"])
    df["valid"] = pos.all(axis=1)
    df["RER"] = (df["S"] * df["P1"] / df["P2"]).where(df["valid"])
    return df


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------

def is_num(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def rate_decimals(x) -> int:
    """4 decimals near 1, 2 for large values, more for very small values."""
    if not is_num(x) or float(x) == 0:
        return 4
    v = abs(float(x))
    if v >= 100:
        return 2
    if v >= 0.01:
        return 4
    return min(10, 3 - int(math.floor(math.log10(v))))


def fmt(x, decimals: int) -> str:
    return f"{float(x):,.{decimals}f}" if is_num(x) else "—"


def tex(x, decimals: int) -> str:
    return fmt(x, decimals).replace(",", "{,}")


def month_label(ts) -> str:
    return pd.Timestamp(ts).strftime("%b %Y")


def esc(text: str) -> str:
    return html.escape(str(text), quote=True)


# --------------------------------------------------------------------------
# Theme and styling
# --------------------------------------------------------------------------

def detect_theme():
    try:
        t = st.context.theme.type
        if t in ("light", "dark"):
            return t
    except Exception:
        pass
    return None


LIGHT_VARS = (
    "--ink:#14213D;--muted:#6B7280;--panel:#F4F6F8;--border:#E3E7EC;"
    "--teal:#0F766E;--amber:#B45309;"
)
DARK_VARS = (
    "--ink:#E8ECF4;--muted:#9CA3AF;--panel:rgba(255,255,255,0.045);"
    "--border:rgba(255,255,255,0.10);--teal:#2DD4BF;--amber:#F59E0B;"
)

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;500;600;700&display=swap');
__VARS__
html, body,
[data-testid="stAppViewContainer"] *:not(.katex):not(.katex *):not(code):not(pre):not([data-testid="stIconMaterial"]) {
  font-family: 'Public Sans', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
}
[data-testid="stMainBlockContainer"] {
  max-width: 1180px;
  padding-top: 2.4rem;
  padding-bottom: 3rem;
}
.rer-title {
  color: var(--ink);
  font-size: clamp(1.65rem, 2.6vw, 2.2rem);
  font-weight: 700;
  letter-spacing: -0.018em;
  line-height: 1.2;
  margin: 0 0 0.4rem 0;
}
.rer-sub {
  color: var(--muted);
  font-size: 1.02rem;
  line-height: 1.55;
  max-width: 46rem;
  margin: 0 0 1.5rem 0;
}
.rer-headline {
  background: var(--panel);
  border: 1px solid var(--border);
  border-left: 4px solid var(--teal);
  border-radius: 12px;
  color: var(--ink);
  font-size: 1.14rem;
  line-height: 1.6;
  margin: 1.5rem 0 0.4rem 0;
  max-width: 62rem;
  padding: 1.05rem 1.3rem;
}
.rer-headline strong { font-weight: 650; font-variant-numeric: tabular-nums; }
.rer-note {
  color: var(--muted);
  font-size: 0.86rem;
  margin: 0.35rem 0 0 0.2rem;
}
.rer-kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 0.85rem;
  margin: 1.3rem 0 1.8rem 0;
}
.rer-kpi {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 1rem 1.15rem 0.95rem 1.15rem;
}
.rer-kpi-label {
  color: var(--muted);
  font-size: 0.84rem;
  font-weight: 600;
  margin-bottom: 0.5rem;
}
.rer-kpi-value {
  color: var(--ink);
  font-size: 1.6rem;
  font-variant-numeric: tabular-nums;
  font-weight: 650;
  line-height: 1.15;
}
.rer-unit {
  color: var(--muted);
  font-size: 0.86rem;
  font-weight: 500;
  margin-left: 0.3rem;
}
.rer-kpi-delta {
  color: var(--ink);
  font-size: 0.86rem;
  font-variant-numeric: tabular-nums;
  margin-top: 0.45rem;
}
.rer-kpi-sub {
  color: var(--muted);
  font-size: 0.8rem;
  font-variant-numeric: tabular-nums;
  margin-top: 0.2rem;
}
.rer-section {
  color: var(--ink);
  font-size: 1.05rem;
  font-weight: 650;
  margin: 1.6rem 0 0.5rem 0;
}
.rer-body { color: var(--ink); line-height: 1.6; max-width: 46rem; }
.rer-body p { margin: 0 0 0.7rem 0; }
.rer-footer {
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: 0.8rem;
  line-height: 1.6;
  margin-top: 2.6rem;
  padding-top: 1rem;
}
[data-testid="stDataFrame"] { font-variant-numeric: tabular-nums; }
@media (max-width: 640px) {
  [data-testid="stMainBlockContainer"] { padding-top: 1.4rem; }
  .rer-headline { font-size: 1.02rem; padding: 0.9rem 1rem; }
  .rer-kpi-value { font-size: 1.35rem; }
}
</style>
"""


def inject_css(theme):
    if theme == "dark":
        vars_css = ":root{" + DARK_VARS + "}"
    elif theme == "light":
        vars_css = ":root{" + LIGHT_VARS + "}"
    else:
        vars_css = (
            ":root{" + LIGHT_VARS + "}"
            "@media (prefers-color-scheme: dark){:root{" + DARK_VARS + "}}"
        )
    st.markdown(CSS.replace("__VARS__", vars_css), unsafe_allow_html=True)


# --------------------------------------------------------------------------
# UI pieces
# --------------------------------------------------------------------------

def swap_countries():
    st.session_state["c1"], st.session_state["c2"] = st.session_state["c2"], st.session_state["c1"]


def kpi_card(label: str, value_html: str, sub: str, delta: str | None = None) -> str:
    parts = [
        '<div class="rer-kpi">',
        f'<div class="rer-kpi-label">{esc(label)}</div>',
        f'<div class="rer-kpi-value">{value_html}</div>',
    ]
    if delta:
        parts.append(f'<div class="rer-kpi-delta">{delta}</div>')
    parts.append(f'<div class="rer-kpi-sub">{esc(sub)}</div>')
    parts.append("</div>")
    return "".join(parts)


def delta_text(change, unit: str, prev_month) -> str:
    if not is_num(change):
        return f"No data for {month_label(prev_month)}"
    arrow = "▲" if change > 0 else ("▼" if change < 0 else "■")
    return f"{arrow} {abs(change):,.1f}{unit} vs {month_label(prev_month)}"


def make_chart(sample, view, s_unit, s_dec, pal, set_font_color):
    x = sample.index
    idx = sample["real_index"]
    s = sample["S"]
    show_real = view in ("Real", "Both")
    show_nom = view in ("Nominal", "Both")
    both = view == "Both"
    fig = go.Figure()

    if show_real:
        base = pd.Series(100.0, index=x).where(idx.notna())
        for band, color in ((idx.clip(lower=100), pal["above"]), (idx.clip(upper=100), pal["below"])):
            fig.add_trace(go.Scatter(
                x=x, y=base, mode="lines", line=dict(width=0),
                hoverinfo="skip", showlegend=False,
            ))
            fig.add_trace(go.Scatter(
                x=x, y=band, mode="lines", line=dict(width=0), fill="tonexty",
                fillcolor=color, hoverinfo="skip", showlegend=False,
            ))
        fig.add_trace(go.Scatter(
            x=x, y=idx, mode="lines", name="Real index (sample average = 100)",
            line=dict(color=pal["teal"], width=2.4), connectgaps=False,
            hovertemplate="Real index: %{y:.1f}<extra></extra>",
        ))
        fig.add_hline(y=100, line=dict(color=pal["muted"], width=1.2, dash="dot"))

    if show_nom:
        fig.add_trace(go.Scatter(
            x=x, y=s, mode="lines", name=f"Nominal rate ({s_unit})",
            yaxis="y2" if both else "y",
            line=dict(color=pal["amber"], width=2.1, dash="dash" if both else "solid"),
            connectgaps=False,
            hovertemplate=f"Nominal: %{{y:,.{s_dec}f}} {s_unit}<extra></extra>",
        ))

    if show_real and idx.notna().any():
        t = idx.last_valid_index()
        v = float(idx[t])
        fig.add_trace(go.Scatter(
            x=[t], y=[v], mode="markers", marker=dict(color=pal["teal"], size=8),
            hoverinfo="skip", showlegend=False,
        ))
        fig.add_annotation(
            x=t, y=v, yref="y", text=f"{v:.1f}", showarrow=True, arrowhead=0,
            arrowwidth=1, arrowcolor=pal["teal"], ax=-36, ay=-30,
            font=dict(color=pal["teal"], size=13),
        )
    if show_nom and s.notna().any():
        t = s.last_valid_index()
        v = float(s[t])
        yref = "y2" if both else "y"
        fig.add_trace(go.Scatter(
            x=[t], y=[v], mode="markers", yaxis=yref,
            marker=dict(color=pal["amber"], size=8, symbol="diamond"),
            hoverinfo="skip", showlegend=False,
        ))
        fig.add_annotation(
            x=t, y=v, yref=yref, text=fmt(v, s_dec), showarrow=True, arrowhead=0,
            arrowwidth=1, arrowcolor=pal["amber"], ax=-36, ay=30 if both else -30,
            font=dict(color=pal["amber"], size=13),
        )

    font = dict(family=FONT_STACK, size=13)
    if set_font_color:
        font["color"] = pal["ink"]
    fig.update_layout(
        height=480,
        margin=dict(l=8, r=8, t=44, b=8),
        hovermode="x unified",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=font,
        hoverlabel=dict(font=dict(family=FONT_STACK)),
        legend=dict(orientation="h", x=0, xanchor="left", y=-0.1, yanchor="top",
                    bgcolor="rgba(0,0,0,0)"),
    )
    fig.update_xaxes(
        type="date", showgrid=True, gridcolor=pal["grid"], hoverformat="%b %Y",
        rangeselector=dict(
            buttons=[
                dict(count=5, label="5Y", step="year", stepmode="backward"),
                dict(count=10, label="10Y", step="year", stepmode="backward"),
                dict(step="all", label="All"),
            ],
            x=1, xanchor="right", y=1.02, yanchor="bottom",
            bgcolor=pal["button"], activecolor=pal["button_active"],
            font=dict(size=12),
        ),
    )

    if view == "Real":
        fig.update_layout(yaxis=dict(
            title=dict(text="Real index (sample average = 100)"),
            gridcolor=pal["grid"], zeroline=False, tickformat=".0f",
        ))
    elif view == "Nominal":
        fig.update_layout(yaxis=dict(
            title=dict(text=s_unit), gridcolor=pal["grid"], zeroline=False, tickformat=",~f",
        ))
    else:
        fig.update_layout(
            yaxis=dict(
                title=dict(text="Real index (avg = 100)", font=dict(color=pal["teal"])),
                tickfont=dict(color=pal["teal"]), gridcolor=pal["grid"],
                zeroline=False, tickformat=".0f",
            ),
            yaxis2=dict(
                title=dict(text=s_unit, font=dict(color=pal["amber"])),
                tickfont=dict(color=pal["amber"]), overlaying="y", side="right",
                showgrid=False, zeroline=False, tickformat=",~f",
            ),
        )
    return fig


def footer(text_html: str):
    st.markdown(f'<div class="rer-footer">{text_html}</div>', unsafe_allow_html=True)


SOURCE_LINE = (
    "Source: International Monetary Fund, CPI and ER datasets via the SDMX 3.0 API "
    "(api.imf.org). Exchange rates are monthly period averages."
)


# --------------------------------------------------------------------------
# Main app
# --------------------------------------------------------------------------

def main():
    st.set_page_config(
        page_title="Real exchange rate explorer",
        page_icon=":material/currency_exchange:",
        layout="wide",
    )
    theme = detect_theme()
    pal = dict(PALETTES["dark" if theme == "dark" else "light"])
    if theme is None:  # unknown theme: use neutral greys that read on both
        pal["grid"] = "rgba(127, 127, 127, 0.16)"
        pal["button"] = "rgba(127, 127, 127, 0.12)"
    inject_css(theme)

    catalog = country_catalog()
    codes = list(catalog)

    def country_label(code: str) -> str:
        return f"{catalog[code]['name']} ({code})"

    if st.session_state.get("c1") not in catalog:
        st.session_state["c1"] = "GBR" if "GBR" in catalog else codes[0]
    if st.session_state.get("c2") not in catalog:
        st.session_state["c2"] = "USA" if "USA" in catalog else codes[1]

    # 1. Header
    st.markdown(
        '<div class="rer-title">Real exchange rate explorer</div>'
        '<div class="rer-sub">How much one currency buys of another country\'s goods, '
        "after adjusting the market exchange rate for inflation in both places. "
        "Live monthly data from the IMF.</div>",
        unsafe_allow_html=True,
    )

    # 2. Controls
    col1, col_swap, col2 = st.columns([10, 1.3, 10], vertical_alignment="bottom")
    with col1:
        st.selectbox("Country 1", codes, key="c1", format_func=country_label)
    with col_swap:
        st.button("⇄", key="swap", on_click=swap_countries, help="Swap Country 1 and Country 2")
    with col2:
        st.selectbox("Country 2", codes, key="c2", format_func=country_label)

    pcol, scol, ecol = st.columns([6, 3, 3], vertical_alignment="bottom")
    with pcol:
        preset = st.segmented_control(
            "Sample period", PRESETS, default="Full history", key="preset"
        ) or "Full history"

    iso1, iso2 = st.session_state["c1"], st.session_state["c2"]
    if iso1 == iso2:
        st.info(
            f"Both selections are {catalog[iso1]['name']}. Choose a different "
            "Country 2 to compare two currencies."
        )
        footer(SOURCE_LINE)
        return

    c1, c2 = catalog[iso1], catalog[iso2]
    n1, n2 = c1["name"], c2["name"]
    cur1, cur2 = c1["currency"], c2["currency"]
    cname1, cname2 = c1["currency_name"], c2["currency_name"]
    if cur1 == cur2:  # e.g. Germany vs euro area: disambiguate labels
        unit1, unit2 = f"{cur1} ({iso1})", f"{cur2} ({iso2})"
        cname1, cname2 = f"{cname1} ({n1})", f"{cname2} ({n2})"
    else:
        unit1, unit2 = cur1, cur2
    s_unit = f"{unit2} per {unit1}"

    def col_unit(u: str) -> str:
        return u.replace(" (", "_").replace(")", "")

    # Data download
    with st.spinner(f"Downloading monthly CPI and exchange rates for {n1} and {n2} from the IMF…"):
        d1 = load_country(iso1)
        d2 = load_country(iso2) if not d1["error"] else None

    failure = d1["error"] or (d2["error"] if d2 else None)
    if failure:
        st.error(
            f"Couldn't get data from the IMF: {failure}. The IMF service is usually back "
            "within a few minutes. Select **Try again**, or pick a different country pair."
        )
        st.button("Try again", type="primary")
        footer(SOURCE_LINE)
        return

    missing = []
    for d, name in ((d1, n1), (d2, n2)):
        if d["cpi"] is None:
            tried = " and ".join(f"`{k}`" for k in d["cpi_tried"])
            missing.append(f"- **CPI for {name} ({d['iso3']})**: tried {tried} in the CPI dataflow.")
        if not d["er_fixed"] and d["er"] is None:
            missing.append(
                f"- **Exchange rate for {name} ({d['iso3']})**: tried `{d['er_key']}` in the ER dataflow."
            )
    if missing:
        st.error(
            "The IMF returned no data for:\n\n" + "\n".join(missing) + "\n\n"
            "The IMF may not publish this series for this economy. Try another country, "
            "or the Euro area (G163) for euro members."
        )
        footer(SOURCE_LINE)
        return

    frame = build_frame(d1, d2)
    if frame is None or not frame["valid"].any():
        def cov(s):
            s = s.dropna() if s is not None else None
            return f"{month_label(s.index.min())}–{month_label(s.index.max())}" if s is not None and len(s) else "none"
        st.error(
            f"There is no month where all four inputs exist for {n1} and {n2}. "
            f"Coverage: CPI {iso1} {cov(d1['cpi'])}, CPI {iso2} {cov(d2['cpi'])}"
            + ("" if d1["er_fixed"] else f", exchange rate {iso1} {cov(d1['er'])}")
            + ("" if d2["er_fixed"] else f", exchange rate {iso2} {cov(d2['er'])}")
            + ". Try a different pair."
        )
        footer(SOURCE_LINE)
        return

    valid_months = frame.index[frame["valid"]]
    first_valid, last_valid = valid_months.min(), valid_months.max()

    # Sample selection
    period_note = None
    end = last_valid
    if preset == "Since 2000":
        start = max(first_valid, pd.Timestamp("2000-01-01"))
        if start > last_valid:
            start = first_valid
            period_note = "Data for this pair ends before 2000, so the full history is shown."
    elif preset == "Last 10 years":
        start = max(first_valid, last_valid - pd.DateOffset(months=119))
    elif preset == "Custom":
        options = [t.strftime("%Y-%m") for t in pd.date_range(first_valid, last_valid, freq="MS")]

        def opt_label(s: str) -> str:
            return month_label(pd.Timestamp(s + "-01"))

        with scol:
            start_s = st.selectbox("Start month", options, index=0,
                                   key=f"start_{iso1}_{iso2}", format_func=opt_label)
        with ecol:
            end_s = st.selectbox("End month", options, index=len(options) - 1,
                                 key=f"end_{iso1}_{iso2}", format_func=opt_label)
        start, end = pd.Timestamp(start_s + "-01"), pd.Timestamp(end_s + "-01")
        if start > end:
            st.warning("The start month is after the end month. Pick an earlier start or a later end.")
            footer(SOURCE_LINE)
            return
    else:
        start = first_valid

    sample = frame.loc[start:end]
    n_total = len(sample)
    n_valid = int(sample["valid"].sum())
    if n_valid == 0:
        st.warning(
            f"No month between {month_label(start)} and {month_label(end)} has all four inputs. "
            "Widen the date range."
        )
        footer(SOURCE_LINE)
        return

    rer_star = float(sample.loc[sample["valid"], "RER"].mean())
    frame["real_index"] = 100.0 * frame["RER"] / rer_star
    sample = frame.loc[start:end]
    range_label = f"{month_label(start)}–{month_label(end)}"

    # 3. Headline
    latest = sample.index[sample["valid"]].max()
    idx_latest = float(sample.at[latest, "real_index"])
    dev = idx_latest - 100.0
    if abs(dev) < 0.05:
        headline = (
            f"In <strong>{esc(month_label(latest))}</strong>, the {esc(cname1)} was right at its "
            f"{esc(range_label)} average in real terms against the {esc(cname2)}."
        )
    else:
        word = "stronger" if dev > 0 else "weaker"
        headline = (
            f"In {esc(month_label(latest))}, the {esc(cname1)} was <strong>{abs(dev):.1f}% {word}</strong> "
            f"in real terms against the {esc(cname2)} than its {esc(range_label)} average."
        )
    st.markdown(f'<div class="rer-headline">{headline}</div>', unsafe_allow_html=True)

    notes = []
    if period_note:
        notes.append(period_note)
    excluded = n_total - n_valid
    if excluded:
        reasons = []
        checks = [("P1", f"CPI {iso1}"), ("P2", f"CPI {iso2}")]
        if not d1["er_fixed"]:
            checks.append(("E1", f"exchange rate {iso1}"))
        if not d2["er_fixed"]:
            checks.append(("E2", f"exchange rate {iso2}"))
        for col, label in checks:
            k = int((~sample[col].gt(0)).sum())
            if k:
                reasons.append(f"{label} missing in {k}")
        notes.append(
            f"{excluded} month{'s' if excluded != 1 else ''} excluded due to missing data"
            + (f" ({'; '.join(reasons)})." if reasons else ".")
        )
    if notes:
        st.markdown(f'<div class="rer-note">{esc(" ".join(notes))}</div>', unsafe_allow_html=True)

    # 4. KPI cards
    s_dec = rate_decimals(sample["S"].median())
    prev = latest - pd.DateOffset(months=12)
    s_latest = float(frame.at[latest, "S"])
    s_prev = frame["S"].get(prev, np.nan)
    i_prev = frame["real_index"].get(prev, np.nan)
    s_change = (s_latest / s_prev - 1) * 100 if is_num(s_prev) and s_prev > 0 else np.nan
    i_change = idx_latest - i_prev if is_num(i_prev) else np.nan
    rer_dec = rate_decimals(rer_star)

    cards = [
        kpi_card(
            "Nominal exchange rate",
            f"{fmt(s_latest, s_dec)}<span class=\"rer-unit\">{esc(s_unit)}</span>",
            month_label(latest),
            delta_text(s_change, "%", prev),
        ),
        kpi_card(
            "Real index",
            fmt(idx_latest, 1),
            f"{month_label(latest)}, sample average = 100",
            delta_text(i_change, " pts", prev),
        ),
        kpi_card(
            "Average real exchange rate (RER*)",
            fmt(rer_star, rer_dec),
            f"Mean RER, {range_label}",
        ),
        kpi_card(
            "Valid months",
            f"{n_valid:,}<span class=\"rer-unit\">of {n_total:,}</span>",
            "Months with all four inputs",
        ),
    ]
    st.markdown(f'<div class="rer-kpis">{"".join(cards)}</div>', unsafe_allow_html=True)

    # 5. Tabs
    tab_chart, tab_data, tab_calc = st.tabs(["Chart", "Data", "How it's calculated"])

    with tab_chart:
        view = st.segmented_control("Show", VIEWS, default="Real", key="view") or "Real"
        fig = make_chart(sample, view, s_unit, s_dec, pal, set_font_color=theme is not None)
        st.plotly_chart(
            fig,
            config={
                "displaylogo": False,
                "responsive": True,
                "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d"],
            },
        )
        st.caption(
            f"Real index above 100: the {cname1} buys more {n2} goods than on average over "
            f"{range_label} (real appreciation). Below 100: it buys fewer. "
            f"The nominal rate is the market price of one {unit1} in {unit2}."
        )

    with tab_data:
        de1 = rate_decimals(sample["E1"].median())
        de2 = rate_decimals(sample["E2"].median())
        export = pd.DataFrame({
            "month": sample.index.strftime("%Y-%m"),
            f"E1_{col_unit(unit1)}_per_USD": sample["E1"].to_numpy(),
            f"E2_{col_unit(unit2)}_per_USD": sample["E2"].to_numpy(),
            f"S_{col_unit(unit2)}_per_{col_unit(unit1)}": sample["S"].to_numpy(),
            f"P1_CPI_{iso1}": sample["P1"].to_numpy(),
            f"P2_CPI_{iso2}": sample["P2"].to_numpy(),
            "RER": sample["RER"].to_numpy(),
            "valid": sample["valid"].to_numpy(),
            "RER_star": rer_star,
            "real_index": sample["real_index"].to_numpy(),
        })
        st.download_button(
            f"Download CSV ({n_total:,} months)",
            data=export.to_csv(index=False).encode("utf-8"),
            file_name=f"rer_{iso1}_{iso2}_{start:%Y%m}_{end:%Y%m}.csv",
            mime="text/csv",
            type="primary",
            icon=":material/download:",
        )
        st.caption(
            f"Every month in {range_label}, including excluded months (valid = False). "
            f"RER* = {fmt(rer_star, rer_dec)}."
        )
        table = pd.DataFrame({
            "Month": [month_label(t) for t in sample.index],
            f"E1 ({unit1} per USD)": sample["E1"].to_numpy(),
            f"E2 ({unit2} per USD)": sample["E2"].to_numpy(),
            f"S ({s_unit})": sample["S"].to_numpy(),
            f"P1 CPI ({iso1})": sample["P1"].to_numpy(),
            f"P2 CPI ({iso2})": sample["P2"].to_numpy(),
            "RER": sample["RER"].to_numpy(),
            "Real index": sample["real_index"].to_numpy(),
            "Valid": np.where(sample["valid"].to_numpy(), "Yes", "No"),
        })
        formats = {
            f"E1 ({unit1} per USD)": f"{{:,.{de1}f}}",
            f"E2 ({unit2} per USD)": f"{{:,.{de2}f}}",
            f"S ({s_unit})": f"{{:,.{s_dec}f}}",
            f"P1 CPI ({iso1})": "{:,.2f}",
            f"P2 CPI ({iso2})": "{:,.2f}",
            "RER": f"{{:,.{rer_dec}f}}",
            "Real index": "{:,.1f}",
        }
        st.dataframe(table.style.format(formats, na_rep="—"), hide_index=True, height=440)

    with tab_calc:
        st.markdown('<div class="rer-section">Formulas</div>', unsafe_allow_html=True)
        st.latex(r"S_t = \frac{E_{2,t}}{E_{1,t}}")
        st.latex(r"RER_t = S_t \times \frac{P_{1,t}}{P_{2,t}}")
        st.latex(r"RER^{*} = \frac{1}{N} \sum_{t \in \text{valid months}} RER_t")
        st.latex(r"\text{Real index}_t = 100 \times \frac{RER_t}{RER^{*}}")

        st.markdown(
            '<div class="rer-body">'
            f"<p>The IMF reports every exchange rate as local currency per US dollar (E). "
            f"Dividing Country 2's rate by Country 1's gives S, the number of {esc(unit2)} "
            f"one {esc(unit1)} buys. A rise in S means the {esc(cname1)} got more expensive "
            "in market terms.</p>"
            "<p>Multiplying S by the ratio of price levels turns it into a real rate: how many "
            f"baskets of {esc(n2)} goods one basket of {esc(n1)} goods is worth. Because each CPI "
            "has its own base year, the level of RER has no meaning on its own, so the app "
            "divides by its average over the selected sample. An index of 110 means the "
            f"{esc(cname1)} is 10% stronger in real terms than its sample average; 90 means 10% weaker.</p>"
            "<p>A month counts only if E1, E2, P1 and P2 all exist and are positive. "
            "Gaps are left as gaps; nothing is interpolated.</p>"
            "</div>",
            unsafe_allow_html=True,
        )

        st.markdown('<div class="rer-section">IMF series used</div>', unsafe_allow_html=True)

        def coverage(s):
            if s is None or s.dropna().empty:
                return "—", 0
            s = s.dropna()
            return f"{month_label(s.index.min())}–{month_label(s.index.max())}", int(s.size)

        rows = []
        for d, e_role, p_role, name in ((d1, "E1", "P1", n1), (d2, "E2", "P2", n2)):
            cov, n = coverage(d["cpi"])
            hicp = d["cpi_key"] and ".HICP." in d["cpi_key"]
            rows.append({
                "Input": f"{p_role} (CPI)",
                "Country": f"{name} ({d['iso3']})",
                "Dataflow": "CPI",
                "Series key": d["cpi_key"],
                "Status": "Found (HICP fallback)" if hicp else "Found",
                "Coverage": cov,
                "Observations": n,
            })
            if d["er_fixed"]:
                rows.append({
                    "Input": f"{e_role} (exchange rate)",
                    "Country": f"{name} ({d['iso3']})",
                    "Dataflow": "—",
                    "Series key": "Not requested",
                    "Status": "Fixed at 1 (USD per USD)",
                    "Coverage": "All months",
                    "Observations": 0,
                })
            else:
                cov, n = coverage(d["er"])
                rows.append({
                    "Input": f"{e_role} (exchange rate)",
                    "Country": f"{name} ({d['iso3']})",
                    "Dataflow": "ER",
                    "Series key": d["er_key"],
                    "Status": "Found",
                    "Coverage": cov,
                    "Observations": n,
                })
        st.dataframe(pd.DataFrame(rows), hide_index=True)

        st.markdown('<div class="rer-section">Check one month by hand</div>', unsafe_allow_html=True)
        valid_opts = [t.strftime("%Y-%m") for t in sample.index[sample["valid"]]]
        pick = st.selectbox(
            "Month to check",
            valid_opts,
            index=len(valid_opts) - 1,
            key=f"check_{iso1}_{iso2}_{start:%Y%m}_{end:%Y%m}",
            format_func=lambda v: month_label(pd.Timestamp(v + "-01")),
        )
        row = sample.loc[pd.Timestamp(pick + "-01")]
        e1, e2, p1, p2 = (float(row[k]) for k in ("E1", "E2", "P1", "P2"))
        s_v, rer_v, ix_v = float(row["S"]), float(row["RER"]), float(row["real_index"])
        dd1, dd2 = rate_decimals(e1) + 2, rate_decimals(e2) + 2
        ds, dr = rate_decimals(s_v) + 2, rate_decimals(rer_v) + 2

        st.latex(
            r"S = \frac{E_2}{E_1} = \frac{" + tex(e2, dd2) + "}{" + tex(e1, dd1) + "} = "
            + tex(s_v, ds) + r"\ \text{" + s_unit + "}"
        )
        st.latex(
            r"RER = S \times \frac{P_1}{P_2} = " + tex(s_v, ds) + r" \times \frac{"
            + tex(p1, 3) + "}{" + tex(p2, 3) + "} = " + tex(rer_v, dr)
        )
        st.latex(
            r"\text{Real index} = 100 \times \frac{RER}{RER^{*}} = 100 \times \frac{"
            + tex(rer_v, dr) + "}{" + tex(rer_star, dr) + "} = " + tex(ix_v, 1)
        )
        st.caption(
            f"E1 = {unit1} per USD, E2 = {unit2} per USD, P1 = CPI {iso1}, P2 = CPI {iso2}. "
            "Inputs are rounded for display; results use full precision."
        )

    # 6. Footer
    footer(
        f"{esc(SOURCE_LINE)}<br>Latest month with complete data for {esc(n1)} and "
        f"{esc(n2)}: {esc(month_label(last_valid))}."
    )


try:
    main()
except Exception as exc:  # last line of defence: never show a traceback
    st.error(
        "Something unexpected went wrong while building the page. Reload the app or "
        f"pick a different country pair. Details: {exc.__class__.__name__}: {exc}"
    )
