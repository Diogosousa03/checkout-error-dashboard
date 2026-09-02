"""Sentry API transport layer.

Low-level functions that talk HTTP to Sentry via httpx. They know about auth,
endpoints, and timeouts — not about our tags or their business meaning. They
return the raw `data` payload; shaping it into DataFrames happens in queries.py.
"""
#sentry alawys returns a 200 OK with a JSON body, even for errors. So we call `raise_for_status()`
from error_dashboard.settings import settings
from error_dashboard.utils import period_to_range, sentry_get

# Base URL for the org-scoped endpoints.
_BASE = f"{settings.sentry_base_url}/api/0/organizations/{settings.sentry_org}"

# Base URL for the project-scoped endpoints (individual events live here — this
# is NOT the Discover events table, so it doesn't need Discover access).
_PROJECT_BASE = (
    f"{settings.sentry_base_url}/api/0/projects/"
    f"{settings.sentry_org}/{settings.sentry_project}"
)

# Base URL for the issue-scoped endpoints. An "issue" is Sentry's grouped error
# (what the Sentry UI shows as one entry); its tag summary and its event list
# hang off this URL. Also outside Discover, so this token can read them.
_ISSUE_BASE = f"{settings.sentry_base_url}/api/0/issues"

# Default page sizes for the issue endpoints.
ISSUE_SEARCH_LIMIT = 25
ISSUE_EVENTS_LIMIT = 25


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


def fetch_project_events(
    query: str, environment: str, period: str, limit: int = 50
) -> list[dict]:
    """List individual events for the project (most recent first).

    Hits /projects/{org}/{project}/events/ — the project event stream, not the
    Discover events table — so each event comes back with its full `tags` array
    ([{"key": ..., "value": ...}, ...]). Returns the raw list of event dicts;
    shaping and any redaction happen in queries.py.
    """
    params = {
        "query": query,
        "environment": environment,
        "statsPeriod": period,
        "full": "true",           # include the tags array on each event
        "per_page": limit,
    }
    resp = sentry_get(
        f"{_PROJECT_BASE}/events/",
        params,
        _headers(),
        "sentry.project_events",
        query=query,
        environment=environment,
        period=period,
    )
    data = resp.json()
    # This endpoint returns a bare JSON array; be tolerant of a wrapped shape too.
    return data if isinstance(data, list) else data.get("data", [])


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


def fetch_project_issues(
    query: str,
    environment: str,
    period: str,
    limit: int = ISSUE_SEARCH_LIMIT,
    sort: str = "freq",
) -> list[dict]:
    """Search the project's issues — Sentry's grouped errors — most frequent first.

    /projects/{org}/{project}/issues/ speaks the same search language as the rest
    of the dashboard (`error_title:"x"`, `!has:flow_phase`, …), which the project
    *event* stream does not: there `query` is only a message substring match, so
    a tag query silently returns nothing. This is therefore the way to go from a
    query to the issue(s) behind it — and it needs no Discover access.

    Its statsPeriod only accepts '' / '24h' / '14d', so the window is sent as an
    explicit start/end pair (see utils.period_to_range).
    """
    start, end = period_to_range(period)
    params = {
        "query": query,
        "environment": environment,
        "start": start,
        "end": end,
        "limit": limit,
        "sort": sort,                 # "freq" = biggest issue first
    }
    resp = sentry_get(
        f"{_PROJECT_BASE}/issues/",
        params,
        _headers(),
        "sentry.project_issues",
        query=query,
        environment=environment,
        period=period,
    )
    data = resp.json()
    return data if isinstance(data, list) else data.get("data", [])


def fetch_issue_tags(
    issue_id: str, environment: str, keys: list[str] | None = None
) -> list[dict]:
    """Tag summary for one issue: each tag it carries plus its top values.

    Returns one dict per tag — {"key", "totalValues", "topValues": [...]}. Only a
    few topValues come back per tag (self-hosted caps it at 3), so the full list
    of a single tag comes from fetch_issue_tag_values(). Pass `keys` to ask for
    specific tags only.
    """
    params: dict = {"environment": environment}
    if keys:
        params["key"] = keys          # httpx repeats it: ?key=a&key=b
    resp = sentry_get(
        f"{_ISSUE_BASE}/{issue_id}/tags/",
        params,
        _headers(),
        "sentry.issue_tags",
        issue_id=issue_id,
        environment=environment,
    )
    data = resp.json()
    return data if isinstance(data, list) else data.get("data", [])


def fetch_issue_tag_values(issue_id: str, key: str, environment: str) -> list[dict]:
    """Every recorded value of one tag on one issue, with its count.

    Rows look like {"value": "adyen", "count": 12, "firstSeen": ..., "lastSeen": ...}
    and come back unsorted, so the caller orders them.
    """
    params = {"environment": environment}
    resp = sentry_get(
        f"{_ISSUE_BASE}/{issue_id}/tags/{key}/values/",
        params,
        _headers(),
        "sentry.issue_tag_values",
        issue_id=issue_id,
        key=key,
        environment=environment,
    )
    data = resp.json()
    return data if isinstance(data, list) else data.get("data", [])


def fetch_issue_events(
    issue_id: str, environment: str, limit: int = ISSUE_EVENTS_LIMIT
) -> list[dict]:
    """Recent individual events of one issue (most recent first), tags included.

    Each event carries its full `tags` array, which is what makes the per-event
    tag view possible. Redaction happens in queries.py, not here.

    Page size goes in `per_page` — this endpoint ignores `limit` (unlike
    /issues/, which only understands `limit`).
    """
    params = {"environment": environment, "per_page": limit}
    resp = sentry_get(
        f"{_ISSUE_BASE}/{issue_id}/events/",
        params,
        _headers(),
        "sentry.issue_events",
        issue_id=issue_id,
        environment=environment,
    )
    data = resp.json()
    return data if isinstance(data, list) else data.get("data", [])
