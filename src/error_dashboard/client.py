"""Sentry API transport layer.

Low-level functions that talk HTTP to Sentry via httpx. They know about auth,
endpoints, and timeouts — not about our tags or their business meaning. They
return the raw `data` payload; shaping it into DataFrames happens in queries.py.
"""
#sentry alawys returns a 200 OK with a JSON body, even for errors. So we call `raise_for_status()`
from error_dashboard.settings import settings
from error_dashboard.utils import sentry_get

# Base URL for the org-scoped endpoints.
_BASE = f"{settings.sentry_base_url}/api/0/organizations/{settings.sentry_org}"


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.sentry_token}"}


def fetch_events(
    fields: list[str], query: str, environment: str, period: str
) -> list[dict]:
    """Call /events/ — aggregated totals grouped by any non-aggregate field.

    Returns a list of rows, e.g. [{"flow_phase": "payment", "count()": 42}, ...].
    """
    params = {
        "field": fields,          # httpx repeats this: ?field=a&field=b
        "query": query,
        "environment": environment,
        "statsPeriod": period,
        "project": settings.sentry_project,
    }
    resp = sentry_get(
        f"{_BASE}/events/",
        params,
        _headers(),
        "sentry.events",
        fields=fields,
        query=query,
        environment=environment,
        period=period,
    )
    return resp.json().get("data", [])


def fetch_tag_values(tag: str, environment: str, period: str) -> list[dict]:
    """Return distinct values and counts for a Sentry tag."""
    params = {
        "environment": environment,
        "statsPeriod": period,
        "project": settings.sentry_project,
    }
    resp = sentry_get(
        f"{_BASE}/tags/{tag}/values/",
        params,
        _headers(),
        "sentry.tag_values",
        tag=tag,
        environment=environment,
        period=period,
    )
    return resp.json()


def fetch_events_stats(
    query: str, environment: str, period: str, interval: str, y_axis: str = "count()"
) -> list:
    """Call /events-stats/ — a time series of `y_axis` bucketed by `interval`.

    `y_axis` is any events-stats metric (default "count()"; e.g.
    "count_unique(user)" for distinct users). Returns the raw series, e.g.
    [[unix_ts, [{"count": N}]], ...] — the value is under "count" whatever the metric.
    """
    params = {
        "yAxis": y_axis,
        "query": query,
        "environment": environment,
        "statsPeriod": period,
        "interval": interval,     # "1d" = one bucket per day
        "project": settings.sentry_project,
    }
    resp = sentry_get(
        f"{_BASE}/events-stats/",
        params,
        _headers(),
        "sentry.events_stats",
        query=query,
        environment=environment,
        period=period,
        interval=interval,
        y_axis=y_axis,
    )
    return resp.json().get("data", [])


def fetch_top_series(
    field: str, query: str, environment: str, period: str, interval: str, top: int
) -> dict:
    """Call /events-stats/ grouped by `field`, returning a series per top group.

    With topEvents set, Sentry returns a dict keyed by the group value:
        {"com.app.one": {"data": [[ts, [{"count": N}]], ...]}, ...}
    So we return the whole JSON dict (the app names are the top-level keys),
    not just a single "data" list.
    """
    params = {
        "yAxis": "count()",
        "field": [field, "count()"],   # group by `field`
        "topEvents": top,              # only the top N groups, each its own series
        "query": query,
        "environment": environment,
        "statsPeriod": period,
        "interval": interval,
        "project": settings.sentry_project,
    }
    resp = sentry_get(
        f"{_BASE}/events-stats/",
        params,
        _headers(),
        "sentry.top_series",
        field=field,
        query=query,
        environment=environment,
        period=period,
        interval=interval,
        top=top,
    )
    return resp.json()
