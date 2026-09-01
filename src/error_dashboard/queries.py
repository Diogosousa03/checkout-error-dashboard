"""Sentry query layer (domain).

Higher-level functions that know *our* tags (flow_phase, error_title, ...) and how
to shape the results into pandas DataFrames for the dashboard. They call the
transport layer in client.py; they never touch httpx directly.
"""

import pandas as pd
import streamlit as st

from error_dashboard.client import fetch_events, fetch_events_stats, fetch_top_series
from error_dashboard.utils import CACHE_TTL_SECONDS, auto_interval, period_to_minutes

# Base Sentry search every query starts from.
BASE_QUERY = "event.type:error"

# "Top apps (last hour)" widget — its fixed window, bucket, and how many apps.
TOP_APPS_PERIOD = "1h"
TOP_APPS_INTERVAL = "5m"
TOP_APPS_LIMIT = 5

# How many groups the per-category series keeps (the rest fold into "Other").
CATEGORY_TOP_N = 8

# Decimal places for a per-minute rate shown in a table.
PER_MINUTE_DECIMALS = 2


@st.cache_data(ttl=CACHE_TTL_SECONDS)  # cached for a while (Streamlit reruns on every interaction)
def errors_by_phase(
    environment: str = "development", period: str = "30d", query: str = BASE_QUERY
) -> pd.DataFrame:
    """Error counts grouped by funnel stage — columns: flow_phase, count()."""
    rows = fetch_events(
        fields=["flow_phase", "count()"],
        query=query,
        environment=environment,
        period=period,
    )
    return pd.DataFrame(rows)


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def top_errors(
    environment: str = "development", period: str = "30d", query: str = BASE_QUERY
) -> pd.DataFrame:
    """Most frequent error titles — columns: error_title, count()."""
    rows = fetch_events(
        fields=["error_title", "count()"],
        query=query,
        environment=environment,
        period=period,
    )
    return pd.DataFrame(rows)


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def errors_over_time(
    environment: str = "development",
    period: str = "30d",
    interval: str = "1d",
    query: str = BASE_QUERY,
) -> pd.DataFrame:
    """Error counts bucketed over time — columns: date, count."""
    data = fetch_events_stats(
        query=query,
        environment=environment,
        period=period,
        interval=interval,
    )
    rows = [
        {"date": pd.to_datetime(ts, unit="s"), "count": buckets[0]["count"] if buckets else 0}
        for ts, buckets in data
    ]
    return pd.DataFrame(rows)


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def affected_users(
    environment: str = "development", period: str = "30d", query: str = BASE_QUERY
) -> int:
    """Distinct users (= wallet addresses) that hit an error in the window.

    user.id = wallet address (setSentryCheckoutContext), so this is "distinct
    wallets affected". Uses count_unique(user) over a single bucket spanning the
    whole period, on /events-stats/ (no Discover events table). You can't sum
    distinct counts across buckets, so a single bucket (interval = period) is the
    correct shape for a window total.
    """
    data = fetch_events_stats(
        query=query,
        environment=environment,
        period=period,
        interval=period,                       # one bucket = the whole window
        y_axis="count_unique(user)",
    )
    values = [buckets[0]["count"] if buckets else 0 for _, buckets in data]
    return int(values[-1]) if values else 0


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def top_apps(
    environment: str = "development",
    period: str = TOP_APPS_PERIOD,
    interval: str = TOP_APPS_INTERVAL,
    limit: int = TOP_APPS_LIMIT,
    query: str = BASE_QUERY,
) -> pd.DataFrame:
    """Apps with the most errors, with a per-app trend series for a sparkline.

    Columns: package_name, total, per_minute, trend (list of bucket counts).
    """
    raw = fetch_top_series(
        field="package_name",
        query=query,
        environment=environment,
        period=period,
        interval=interval,
        top=limit,
    )

    # Sentry returns two shapes:
    #  - grouped:  {"<app>": {"data": [...]}, ...}          (multiple groups)
    #  - flat:     {"data": [...], "meta": {...}, ...}       (single/implicit group)
    if isinstance(raw.get("data"), list):
        groups = {"": raw["data"]}                                  # flat -> one unnamed group
    else:
        groups = {name: series.get("data", []) for name, series in raw.items()}

    period_minutes = period_to_minutes(period)
    rows = []
    for app, data in groups.items():
        counts = [buckets[0]["count"] if buckets else 0 for _, buckets in data]
        total = sum(counts)
        rows.append({
            "package_name": app or "(no app tag)",
            "total": total,
            "per_minute": round(total / period_minutes, PER_MINUTE_DECIMALS),
            "trend": counts,                       # the sparkline data
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values("total", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------- #
#  Error-rate views (all built on /events-stats/ — no Discover events table)  #
#  These are the Error-rates screen and intentionally stay unfiltered.        #
# --------------------------------------------------------------------------- #


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def category_series(
    field: str,
    environment: str = "development",
    period: str = "30d",
    interval: str = "1d",
    top: int = CATEGORY_TOP_N,
    query: str = BASE_QUERY,
) -> pd.DataFrame:
    """Per-category error time series, grouped by `field` (e.g. flow_phase).

    Uses /events-stats/ with topEvents grouping (same mechanism as top_apps),
    so it never touches the Discover /events/ table. Returns a tidy long-form
    DataFrame — columns: category, date, count — ready to pivot into rates.
    Pass `query` to scope it (e.g. to one package_name).
    """
    raw = fetch_top_series(
        field=field,
        query=query,
        environment=environment,
        period=period,
        interval=interval,
        top=top,
    )

    # Same two shapes as top_apps: grouped dict, or a single flat series.
    if isinstance(raw.get("data"), list):
        groups = {"": raw["data"]}
    else:
        groups = {name: series.get("data", []) for name, series in raw.items()}

    rows = []
    for category, data in groups.items():
        for ts, buckets in data:
            rows.append({
                "category": category or "(none)",
                "date": pd.to_datetime(ts, unit="s"),
                "count": buckets[0]["count"] if buckets else 0,
            })
    return pd.DataFrame(rows)


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def errors_by_app(
    environment: str = "development", period: str = "30d", query: str = BASE_QUERY
) -> pd.DataFrame:
    """Error counts grouped by package_name — columns: package_name, count().

    Uses /events/ (like top_errors), so it returns ALL apps — no top-N cutoff,
    unlike category_series which folds everything beyond the top into "Other".
    """
    rows = fetch_events(
        fields=["package_name", "count()"],
        query=query,
        environment=environment,
        period=period,
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.sort_values("count()", ascending=False).reset_index(drop=True)
    return df


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def rate_summary(environment: str = "development", period: str = "30d") -> dict:
    """Current vs previous error rate over the selected period.

    Requests a double-length series and splits it in half: the first half is the
    previous period, the second half the current one. Returns totals, the
    current errors/min, the percentage change, and the current-half series (for
    a sparkline). All from /events-stats/.
    """
    value, unit = int(period[:-1]), period[-1]
    double = f"{value * 2}{unit}"                  # 2x period → previous + current halves
    interval = auto_interval(period)

    data = fetch_events_stats(
        query=BASE_QUERY,
        environment=environment,
        period=double,
        interval=interval,
    )
    series = [
        {"date": pd.to_datetime(ts, unit="s"), "count": buckets[0]["count"] if buckets else 0}
        for ts, buckets in data
    ]

    mid = len(series) // 2                          # split point: previous | current
    prev_total = sum(row["count"] for row in series[:mid])
    curr_total = sum(row["count"] for row in series[mid:])

    minutes = period_to_minutes(period)
    curr_per_min = curr_total / minutes if minutes else 0.0
    prev_per_min = prev_total / minutes if minutes else 0.0
    delta_pct = ((curr_total - prev_total) / prev_total * 100) if prev_total else None

    return {
        "curr_total": curr_total,
        "prev_total": prev_total,
        "curr_per_min": curr_per_min,
        "prev_per_min": prev_per_min,
        "delta_pct": delta_pct,
        "current": pd.DataFrame(series[mid:]),   # current-half series for the sparkline
    }


if __name__ == "__main__":
    # Quick manual test:  uv run python -m error_dashboard.queries
    print(errors_by_phase())
