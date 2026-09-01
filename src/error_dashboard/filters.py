"""Tag filtering — build a Sentry query from sidebar selections.

The tags we standardized (see Week4_Error_Structure.md) are all Sentry tags or
built-in fields, so filtering is just appending `key:value` terms to the base
query. Option lists are loaded live from Sentry (via the tagstore tag-values
endpoint, which does not require Discover). This module has no UI — app.py
renders the widgets and passes the collected selections to build_query().
"""

import streamlit as st

from error_dashboard.client import fetch_tag_values
from error_dashboard.queries import BASE_QUERY
from error_dashboard.utils import CACHE_TTL_SECONDS

# Sentinel for the "(unset)" choice — filters for events missing the tag.
NONE = "__none__"

# Cap on how many distinct values a single dropdown loads (highest-count first).
TAG_VALUE_LIMIT = 100

# Low / medium-cardinality tags → multiselect dropdowns, options auto-loaded.
# (label, sentry_key, allow_unset)
FILTER_TAGS = [
    ("Flow phase",     "flow_phase",       True),
    ("Severity",       "level",            False),
    ("Payment method", "payment_method",   False),
    ("Payflow",        "payflow",          False),
    ("Country",        "geo_country",      False),
    ("App",            "package_name",     False),
    ("Payment funnel", "payment_funnel",   False),
    ("HTTP status",    "http_status_code", False),
]

# Very high-cardinality identifiers → free-text drill-down, NOT dropdowns.
# (label, sentry_key)
SEARCH_TAGS = [
    ("Transaction UID", "transaction_uid"),
    ("Wallet (user.id)", "user.id"),
]


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def tag_values(tag: str, environment: str, period: str, limit: int = TAG_VALUE_LIMIT) -> list[str]:
    """Distinct values for a tag, most frequent first (options for a dropdown).

    Uses the tagstore tag-values endpoint (no Discover needed). Resilient: if a
    tag can't be loaded (unknown tag, permissions, network), returns [] so one
    bad facet never crashes the sidebar — that dropdown just shows no options.
    """
    try:
        rows = fetch_tag_values(tag=tag, environment=environment, period=period)
    except Exception:
        return []
    if not rows:
        return []
    rows = sorted(rows, key=lambda r: r.get("count", 0), reverse=True)
    values = [str(r.get("value")) for r in rows if r.get("value") not in (None, "", "null")]
    return values[:limit]


def _quote(value: str) -> str:
    """Wrap a value in double quotes only if it needs it (spaces / specials)."""
    if any(c in value for c in ' :"()[]'):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _term(key: str, values: list[str]) -> str | None:
    """One query term for a tag and its selected values (OR within the tag)."""
    if not values:
        return None
    if values == [NONE]:
        return f"!has:{key}"                       # "is not set"
    if len(values) == 1:
        return f"{key}:{_quote(values[0])}"
    joined = ", ".join(_quote(v) for v in values)
    return f"{key}:[{joined}]"                      # Sentry IN-syntax; see note


def build_query(
    selections: dict[str, list[str]], searches: dict[str, str], base: str = BASE_QUERY
) -> str:
    """Assemble the full Sentry query from all selections.

    selections: {sentry_key: [values]}  — from the dropdowns
    searches:   {sentry_key: "value"}   — from the text inputs (drill-down)
    base:       the query to build on (default event.type:error); pass an already
                filtered query to AND a second, table-local filter onto it.
    Multiple tags AND together; multiple values within a tag OR.

    Note: `key:[a, b]` is Sentry's IN-syntax and works on modern Sentry. If the
    self-hosted sentry02 rejects it, change the last line of _term() to build
    "(" + " OR ".join(f"{key}:{_quote(v)}" for v in values) + ")".
    """
    parts = [base]
    for key, values in selections.items():
        term = _term(key, values)
        if term:
            parts.append(term)
    for key, value in searches.items():
        value = (value or "").strip()
        if value:
            parts.append(f"{key}:{_quote(value)}")
    return " ".join(parts)
