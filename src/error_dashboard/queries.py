"""Sentry query layer (domain).

Higher-level functions that know *our* tags (flow_phase, error_title, ...) and how
to shape the results into pandas DataFrames for the dashboard. They call the
transport layer in client.py; they never touch httpx directly.
"""

import pandas as pd
import streamlit as st

from error_dashboard.client import (
    fetch_events,
    fetch_events_stats,
    fetch_issue_events,
    fetch_issue_tag_values,
    fetch_issue_tags,
    fetch_project_events,
    fetch_project_issues,
    fetch_top_series,
)
from error_dashboard.settings import settings
from error_dashboard.utils import CACHE_TTL_SECONDS, auto_interval, period_to_minutes

# Base Sentry search every query starts from.
BASE_QUERY = "event.type:error"

# How many individual events to pull for the per-error event list.
ERROR_EVENTS_LIMIT = 25

# Health/heartbeat check: window of hourly buckets used to detect a silence.
HEALTH_WINDOW = "48h"
HEALTH_INTERVAL = "1h"

# Tag keys that identify a user — NEVER surfaced. user.id = wallet address
# (setSentryCheckoutContext), so it's dropped from every event's tag set, same
# spirit as the EWT rule: the dashboard shows the wallet to no one.
REDACTED_TAG_KEYS = {"user", "user.id", "wallet", "wallet_address", "user.username", "user.email"}

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
def error_events(
    environment: str = "development",
    period: str = "30d",
    query: str = BASE_QUERY,
    limit: int = ERROR_EVENTS_LIMIT,
) -> list[dict]:
    """Individual events matching `query` — one dict per event, wallet redacted.

    Shape: [{"event_id", "timestamp", "title", "tags": {key: value}}, ...], most
    recent first. Any user-identifying tag (user.id = wallet address) is stripped
    here so it can never reach the UI. Raises on transport errors so the caller can
    surface the real reason (403 vs 404) instead of a silent empty list.

    Careful: on the project event stream `query` is only a substring match on the
    message — a tag query (`error_title:"x"`, or even the base `event.type:error`)
    matches nothing there and this returns []. To get the events behind a tag
    query, resolve it to an issue first (issues_for_query) and then read
    issue_events().
    """
    raw = fetch_project_events(
        query=query, environment=environment, period=period, limit=limit
    )

    events = []
    for ev in raw:
        tags = {}
        for tag in (ev.get("tags") or []):
            key = tag.get("key")
            if key is None or key in REDACTED_TAG_KEYS:
                continue                       # never surface user-identifying tags
            tags[key] = tag.get("value")
        event_id = ev.get("eventID") or ev.get("id")
        events.append({
            "event_id": event_id,
            "timestamp": ev.get("dateCreated") or ev.get("dateReceived"),
            "title": ev.get("title") or ev.get("message"),
            "tags": tags,
            "sentry_url": _sentry_event_url(ev.get("groupID"), event_id),
        })
    return events


def _sentry_event_url(group_id, event_id) -> str | None:
    """Deep-link to a specific event (or its issue) in the Sentry UI, or None."""
    org = settings.sentry_org
    base = settings.sentry_base_url.rstrip("/")
    if group_id and event_id:
        return f"{base}/organizations/{org}/issues/{group_id}/events/{event_id}/"
    if group_id:
        return f"{base}/organizations/{org}/issues/{group_id}/"
    return None


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def rate_summary(
    environment: str = "development", period: str = "30d", query: str = BASE_QUERY
) -> dict:
    """Current vs previous error rate over the selected period.

    Requests a double-length series and splits it in half: the first half is the
    previous period, the second half the current one. Returns totals, the
    current errors/min, the percentage change, and the current-half series (for
    a sparkline). All from /events-stats/. Pass `query` to scope the trend to the
    active filters (defaults to the unfiltered base query).
    """
    value, unit = int(period[:-1]), period[-1]
    double = f"{value * 2}{unit}"                  # 2x period → previous + current halves
    interval = auto_interval(period)

    data = fetch_events_stats(
        query=query,
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


def sentry_health(
    environment: str = "production",
    window: str = HEALTH_WINDOW,
    interval: str = HEALTH_INTERVAL,
) -> dict:
    """Heartbeat: is Sentry still receiving errors, or has it gone silent?

    Reads hourly buckets over `window` (unfiltered, on /events-stats/) and reports
    how many trailing hours are at zero and the baseline rate before that silence.
    The caller decides whether that combination means "probably down" — this just
    supplies the facts. A checkout flow always has a background error rate, so a
    run of zero hours against a non-zero baseline is the "Sentry may be down" signal.

    Returns {hours_silent, baseline_per_hour, buckets}. hours_silent counts only
    completed buckets (the in-progress current hour is dropped, so the start of an
    hour doesn't look like a fresh silence). baseline_per_hour is the mean over the
    hours *before* the silence began (0.0 if there's no active history to compare).
    """
    df = errors_over_time(environment=environment, period=window,
                          interval=interval, query=BASE_QUERY)
    if df.empty:
        return {"hours_silent": 0, "baseline_per_hour": 0.0, "buckets": 0}

    counts = df.sort_values("date")["count"].tolist()
    if len(counts) > 1:
        counts = counts[:-1]                    # drop the incomplete current hour

    hours_silent = 0
    for count in reversed(counts):
        if count == 0:
            hours_silent += 1
        else:
            break

    active = counts[:len(counts) - hours_silent]    # the hours before the silence
    baseline = sum(active) / len(active) if active else 0.0
    return {"hours_silent": hours_silent, "baseline_per_hour": baseline, "buckets": len(counts)}


# --------------------------------------------------------------------------- #
#  Per-error tag values (via Sentry issues — no Discover access needed)       #
#                                                                             #
#  The views above answer "how many"; these answer "with WHAT VALUES". A       #
#  clicked error_title is resolved to the Sentry issue(s) behind it, and the   #
#  issue's tag summary / event list supply the actual tag values.             #
# --------------------------------------------------------------------------- #

# How many issues a single error query may resolve to (usually 1).
ISSUE_SEARCH_LIMIT = 25

# How many individual events of an issue the per-event tag view lists.
ISSUE_EVENTS_LIMIT = 25

# Shown instead of an empty tag value — the tag exists on the issue, but this
# slice of events didn't set it.
TAG_UNSET_LABEL = "(not set)"


def _sentry_issue_url(issue_id) -> str | None:
    """Deep-link to an issue in the Sentry UI, or None."""
    if not issue_id:
        return None
    base = settings.sentry_base_url.rstrip("/")
    return f"{base}/organizations/{settings.sentry_org}/issues/{issue_id}/"


def _tag_value_label(value) -> str:
    """Display form of a raw tag value ('' -> "(not set)")."""
    text = "" if value is None else str(value)
    return text if text else TAG_UNSET_LABEL


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def issues_for_query(
    query: str = BASE_QUERY,
    environment: str = "development",
    period: str = "30d",
    limit: int = ISSUE_SEARCH_LIMIT,
) -> list[dict]:
    """The issues matching a query, biggest first — the bridge to the tag views.

    Pass the same query a view is already scoped to (e.g. the global filters plus
    error_title:"x") and get back the grouped errors behind it, each with its
    event/user counts and a Sentry deep-link.
    """
    rows = fetch_project_issues(
        query=query, environment=environment, period=period, limit=limit
    )
    return [
        {
            "id": row.get("id"),
            "short_id": row.get("shortId"),
            "title": row.get("title") or row.get("culprit") or "(untitled)",
            "culprit": row.get("culprit"),
            "level": row.get("level"),
            "count": int(row.get("count") or 0),
            "users": int(row.get("userCount") or 0),
            "first_seen": row.get("firstSeen"),
            "last_seen": row.get("lastSeen"),
            "sentry_url": row.get("permalink") or _sentry_issue_url(row.get("id")),
        }
        for row in rows
    ]


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def issue_tag_summary(issue_id: str, environment: str = "development") -> pd.DataFrame:
    """One row per tag on this issue — columns: tag, top_value, top_count, share.

    `share` is the top value's fraction of the issue's tagged events, so a tag at
    1.00 is constant for this error (a fingerprint) and a low share means the
    error spreads across many values. User-identifying tags are dropped here, so
    the wallet address can never reach the UI.
    """
    rows = fetch_issue_tags(issue_id=issue_id, environment=environment)

    summary = []
    for row in rows:
        key = row.get("key")
        if key is None or key in REDACTED_TAG_KEYS:
            continue                              # never surface user-identifying tags
        values = sorted(
            row.get("topValues") or [],
            key=lambda value: value.get("count") or 0,
            reverse=True,
        )
        top = values[0] if values else {}
        count = int(top.get("count") or 0)
        total = int(row.get("totalValues") or 0)
        summary.append({
            "tag": key,
            "top_value": _tag_value_label(top.get("value")),
            "top_count": count,
            "share": (count / total) if total else 0.0,
            "events": total,
        })

    df = pd.DataFrame(summary)
    if df.empty:
        return df
    # Constant tags (share 1.0) first — they characterise the error best.
    return df.sort_values(["share", "events"], ascending=False).reset_index(drop=True)


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def issue_tag_values(
    issue_id: str, key: str, environment: str = "development"
) -> pd.DataFrame:
    """Every value of one tag on one issue — columns: value, count, share, last_seen.

    Needed because the issue tag summary only returns a handful of top values per
    tag. Refuses to read a user-identifying tag at all.
    """
    if key in REDACTED_TAG_KEYS:
        return pd.DataFrame()

    rows = fetch_issue_tag_values(issue_id=issue_id, key=key, environment=environment)
    total = sum(int(row.get("count") or 0) for row in rows)

    values = [
        {
            "value": _tag_value_label(row.get("value")),
            "count": int(row.get("count") or 0),
            "share": (int(row.get("count") or 0) / total) if total else 0.0,
            "last_seen": row.get("lastSeen"),
        }
        for row in rows
    ]
    df = pd.DataFrame(values)
    if df.empty:
        return df
    return df.sort_values("count", ascending=False).reset_index(drop=True)


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def issue_events(
    issue_id: str, environment: str = "development", limit: int = ISSUE_EVENTS_LIMIT
) -> list[dict]:
    """Recent events of one issue with their tag sets, most recent first.

    Shape: [{"event_id", "timestamp", "title", "tags": {key: value}, "sentry_url"}].
    This is the raw truth behind the aggregates — the exact tag values Sentry
    received for one occurrence — with every user-identifying tag stripped.
    """
    raw = fetch_issue_events(issue_id=issue_id, environment=environment, limit=limit)

    events = []
    for ev in raw:
        tags = {
            tag["key"]: tag.get("value")
            for tag in (ev.get("tags") or [])
            if tag.get("key") and tag["key"] not in REDACTED_TAG_KEYS
        }
        event_id = ev.get("eventID") or ev.get("id")
        events.append({
            "event_id": event_id,
            "timestamp": ev.get("dateCreated") or ev.get("dateReceived"),
            "title": ev.get("title") or ev.get("message"),
            "tags": tags,
            "sentry_url": _sentry_event_url(ev.get("groupID") or issue_id, event_id),
        })
    return events


if __name__ == "__main__":
    # Quick manual test:  uv run python -m error_dashboard.queries
    print(errors_by_phase())
