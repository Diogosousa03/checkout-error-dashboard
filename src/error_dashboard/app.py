"""Streamlit app — entry point and the Overview view.

  - Overview: metrics, error counts over time, top apps, error list (filterable)
  - Error rates: lives in error_rates.py; imported and rendered here.

Global controls: view, environment (production/development), time range.
The tag filters are shown only in the Overview view.
Run with:  uv run streamlit run src/error_dashboard/app.py
"""

import json
import traceback

import altair as alt
import httpx
import pandas as pd
import streamlit as st

from error_dashboard.queries import (
    affected_users,
    category_series,
    errors_over_time,
    issue_events,
    issue_tag_summary,
    issue_tag_values,
    issues_for_query,
    sentry_health,
    top_apps,
    top_errors,
    TOP_APPS_PERIOD,
    TOP_APPS_INTERVAL,
)
from error_dashboard.filters import (
    FILTER_TAGS,
    SEARCH_TAGS,
    NONE,
    BASE_QUERY,
    tag_values,
    build_query,
)
from error_dashboard.utils import (
    auto_interval,
    granularity_label,
    period_to_minutes,
    SentryCircuitOpen,
)
from error_dashboard.error_rates import display_error_rates
from error_dashboard.insights import (
    build_facts,
    generate_insights,
    InsightsUnavailable,
)

# Global control options (the fixed choices; the user's selection lives in main()).
VIEWS = ["Overview", "Error rates"]
ENVIRONMENTS = ["production", "development"]   # production is the default (first)
PERIODS = ["7d", "30d", "90d"]
ERRORS_PER_PAGE = 3

# Label shown for the "is not set" choice on tags that allow it (e.g. flow_phase).
UNSET_LABEL = "(unset)"

# --- Overview chart geometry / layout ---
BAR_CHART_HEIGHT_PX = 200
PAGINATION_COL_RATIO = [1, 2, 1]   # prev / info / next
APP_DETAIL_HEIGHT_PX = 180         # per-app drill-down charts
APP_DETAIL_AREA_OPACITY = 0.35
# Sentry sends its top-N rollup group as "Other"; we display it (everywhere in
# the app) as "Others" — it's the *rest* of the values combined, i.e. plural.
SENTRY_ROLLUP_RAW = "Other"
OTHER_LABEL = "Others"
NO_APP_TAG_LABEL = "(no app tag)"
SYNTHETIC_APPS = {OTHER_LABEL, NO_APP_TAG_LABEL}   # rows that aren't a real package_name

# --- Per-error drill-down (click a row in the Errors list) ---
ERROR_DETAIL_HEIGHT_PX = 180

# --- Tag values for one error (the "with what values" half of the detail view) ---
TAG_SUMMARY_MAX_HEIGHT_PX = 320
# Cap on how many issues the picker offers when one error_title groups into several.
ISSUE_PICKER_MAX = 10
# Tags shown as columns in the per-event table, in this order; any other tag the
# events carry is appended after them, so nothing is hidden.
EVENT_TAG_COLUMNS = [
    "level", "flow_phase", "payment_method", "payflow", "geo_country",
    "package_name", "http_status_code", "transaction_uid",
]
EVENT_TAG_MISSING = "—"        # this event doesn't carry that tag at all
EVENT_TIME_COLUMN = "when"

# --- "Group by" sub-view: the dimension is the USER's choice, not fixed ---
# label -> sentry tag/field. The user picks one; the table shows its top values.
GROUP_BY_FIELDS = {
    "Flow phase":     "flow_phase",
    "Payment method": "payment_method",
    "Country":        "geo_country",
    "App":            "package_name",
    "Severity":       "level",
    "Payflow":        "payflow",
    "Payment funnel": "payment_funnel",
    "HTTP status":    "http_status_code",
}
GROUP_ROUND_DECIMALS = 2
# category_series labels the empty-key group "(none)"; Sentry's top-N rollup "Other"
# (which we relabel to "Others" when building the table).
GROUP_NONE_LABEL = "(none)"
GROUP_OTHER_LABEL = OTHER_LABEL

# --- HTTP status handling ---
HTTP_FORBIDDEN = 403
SERVER_ERROR_STATUS = (500, 502, 503, 504)

# --- Insights panel ---
SEVERITY_DOT = {"info": "🟢", "warning": "🟡", "critical": "🔴"}
CONFIDENCE_LABEL = {"low": "Low", "medium": "Medium", "high": "High"}

# --- Sentry heartbeat banner ---
# Warn when this many recent hours are silent AND the baseline says errors were
# expected. A checkout flow always has a background rate, so a run of zero hours
# against a non-zero baseline most likely means Sentry stopped ingesting.
SILENCE_THRESHOLD_HOURS = 2
HEALTH_MIN_EXPECTED = 5      # expected events over the silent window to bother warning


def configure_page() -> None:
    """Configure the Streamlit page."""
    st.set_page_config(
        page_title="Error Dashboard",
        page_icon=":bar_chart:",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    # Trim Streamlit's default top padding so the title sits near the top.
    st.markdown(
        "<style>.block-container{padding-top:2rem;}</style>",
        unsafe_allow_html=True,
    )



def render_tag_filters(container, key_prefix: str, environment: str, period: str,
                       base: str = BASE_QUERY, heading: str | None = None) -> str:
    """Render the "Filter by" picker + per-tag dropdowns into `container`.

    Reusable so several instances coexist (the sidebar, a per-table filter, …) —
    widget keys are namespaced by `key_prefix`. Lazy: only chosen tags fetch their
    values from Sentry. Returns a query built on `base`, so a table-local filter
    ANDs onto the global one when you pass base=global_query.
    """
    if heading:
        container.markdown(heading)

    # Local only — no network. Just picks WHICH tags to filter by.
    tag_labels = [label for label, _, _ in FILTER_TAGS]
    active = container.multiselect(
        "Filter by", tag_labels, key=f"{key_prefix}_active",
        help="Pick a tag to load its values from Sentry.",
    )
    by_label = {label: (key, allow_unset) for label, key, allow_unset in FILTER_TAGS}

    selections: dict[str, list[str]] = {}
    for label in active:
        key, allow_unset = by_label[label]
        options = tag_values(key, environment, period)   # fetched ONLY for chosen tags
        if allow_unset:
            options = [UNSET_LABEL] + options
        chosen = container.multiselect(label, options, key=f"{key_prefix}_{key}")
        # Map the "(unset)" label back to the sentinel the builder understands.
        selections[key] = [NONE if c == UNSET_LABEL else c for c in chosen]

    searches: dict[str, str] = {}
    with container.expander("Drill-down (exact match)"):
        for label, key in SEARCH_TAGS:
            searches[key] = st.text_input(label, key=f"{key_prefix}_search_{key}")

    return build_query(selections, searches, base=base)



def display_error_metrics(environment: str, period: str, query: str) -> None:
    """Headline metrics: total errors and errors per minute over the period."""
    df = errors_over_time(environment=environment, period=period, interval="1d", query=query)
    total = int(df["count"].sum()) if not df.empty else 0
    minutes = period_to_minutes(period)
    per_minute = total / minutes if minutes else 0.0
    users = affected_users(environment=environment, period=period, query=query)

    col1, col2, col3 = st.columns(3)
    col1.metric("Total errors", f"{total:,}")
    col2.metric("Errors / min", f"{per_minute:.2f}")
    col3.metric("Affected users", f"{users:,}")



def _render_insights(result: dict, facts: dict) -> None:
    """Render a generated insights result: lines, action, confidence, facts."""
    for ins in result.get("insights", []):
        dot = SEVERITY_DOT.get(ins.get("severity_hint"), "⚪")
        note = " _(hypothesis)_" if ins.get("kind") == "hypothesis" else ""
        st.markdown(f"{dot} {ins.get('text', '')}{note}")

    action = result.get("recommended_action")
    if action:
        st.info(f"**Recommended action:** {action}")

    conf = CONFIDENCE_LABEL.get(result.get("confidence"), "—")
    caveats = result.get("caveats", "")
    st.caption(f"Confidence: {conf}. {caveats}")

    with st.expander("Facts used"):
        st.json(facts)   # exactly what the model saw — every claim is checkable


def display_insights_panel(environment: str, period: str, query: str) -> None:
    """On-demand AI read of the current state — never auto-runs, never sees users.

    Builds an aggregates-only facts payload from the current view and asks Claude
    for 3–5 grounded insights + one action. Failures show a friendly message, not
    a traceback (same pattern as the Sentry path).
    """
    with st.expander("✨ AI insights (beta)", expanded=False):
        st.caption(
            "A short, grounded read of the current state — generated on demand "
            "from the aggregates above. It only ever sees counts and rates, never "
            "any user data."
        )
        if st.button("Generate insights", key="gen_insights"):
            try:
                facts = build_facts(environment, period, query)
                result = generate_insights(json.dumps(facts, default=str))
                st.session_state["insights"] = {"result": result, "facts": facts}
                st.session_state.pop("insights_msg", None)
            except InsightsUnavailable as exc:
                st.session_state.pop("insights", None)
                st.session_state["insights_msg"] = ("info", str(exc))
            except Exception:                          # API / network / parse issue
                st.session_state.pop("insights", None)
                st.session_state["insights_msg"] = (
                    "warning",
                    "Couldn't generate insights just now (the model call failed "
                    "or timed out). Try again in a moment.",
                )

        msg = st.session_state.get("insights_msg")
        if msg:
            (st.info if msg[0] == "info" else st.warning)(msg[1])

        saved = st.session_state.get("insights")
        if saved:
            _render_insights(saved["result"], saved["facts"])


def bar_chart_error_counts(environment: str, period: str, query: str) -> None:
    """Display a bar chart of error counts over time."""
    st.subheader("Error Counts Over Time")
    interval = auto_interval(period)
    granularity = granularity_label(interval)
    df = errors_over_time(environment=environment, period=period, interval=interval, query=query)

    # Show the hour in the tooltip only when buckets are hourly.
    tooltip_fmt = "%b %d, %H:%M" if interval == "1h" else "%b %d, %Y"

    chart = (
        alt.Chart(df)
        .mark_bar()
        .encode(
            x=alt.X("date:T", title=f"Date ({granularity})"),
            y=alt.Y("count:Q", title="Errors"),
            tooltip=[
                alt.Tooltip("date:T", title="Time", format=tooltip_fmt),
                alt.Tooltip("count:Q", title="Errors"),
            ],
        )
        .properties(height=BAR_CHART_HEIGHT_PX)
    )
    st.altair_chart(chart, use_container_width=True)



def display_app_view(app: str, environment: str, period: str, base_query: str) -> None:
    """Full sub-view for one app (last hour), reached by clicking a Top-apps row.

    "Back" returns to the Overview. All on /events-stats/ (no Discover). "Other"
    is a Sentry rollup of apps beyond the top N and isn't a single package_name,
    so it can't be drilled; "(no app tag)" is drilled as errors with no
    package_name set.
    """
    if st.button("← Back to overview"):
        st.session_state.pop("drill_app", None)
        # New table key on return so the old row selection doesn't re-navigate.
        st.session_state["top_apps_gen"] = st.session_state.get("top_apps_gen", 0) + 1
        st.rerun()

    st.header(f"App detail — {app}")

    if app == OTHER_LABEL:
        st.info("“Others” is every app beyond the top 5 combined — go back and pick a named app.")
        return
    if app == NO_APP_TAG_LABEL:
        app_query = f"{base_query} !has:package_name"
    else:
        app_query = f'{base_query} package_name:"{app}"'

    st.caption("Last hour")

    over = errors_over_time(environment=environment, period=TOP_APPS_PERIOD,
                            interval=TOP_APPS_INTERVAL, query=app_query)
    total = int(over["count"].sum()) if not over.empty else 0
    minutes = period_to_minutes(TOP_APPS_PERIOD)
    per_min = total / minutes if minutes else 0.0
    users = affected_users(environment=environment, period=TOP_APPS_PERIOD, query=app_query)

    c1, c2, c3 = st.columns(3)
    c1.metric("Errors (1h)", f"{total:,}")
    c2.metric("Errors / min", f"{per_min:.2f}")
    c3.metric("Affected users", f"{users:,}")

    # Errors over the last hour (5-min buckets).
    if not over.empty:
        area = (
            alt.Chart(over)
            .mark_area(opacity=APP_DETAIL_AREA_OPACITY, line=True)
            .encode(
                x=alt.X("date:T", title="Time (5-min buckets)"),
                y=alt.Y("count:Q", title="Errors"),
                tooltip=[
                    alt.Tooltip("date:T", title="Time", format="%b %d, %H:%M"),
                    alt.Tooltip("count:Q", title="Errors"),
                ],
            )
            .properties(height=APP_DETAIL_HEIGHT_PX)
        )
        st.altair_chart(area, use_container_width=True)

    # Which funnel phase is failing for this app.
    phases = category_series(field="flow_phase", environment=environment,
                             period=TOP_APPS_PERIOD, interval=TOP_APPS_INTERVAL, query=app_query)
    if not phases.empty:
        by_phase = phases.groupby("category", as_index=False)["count"].sum()
        if by_phase["count"].sum() > 0:
            st.caption("By flow phase")
            bar = (
                alt.Chart(by_phase)
                .mark_bar()
                .encode(
                    x=alt.X("count:Q", title="Errors"),
                    y=alt.Y("category:N", sort="-x", title="Flow phase"),
                    tooltip=[
                        alt.Tooltip("category:N", title="Flow phase"),
                        alt.Tooltip("count:Q", title="Errors"),
                    ],
                )
                .properties(height=APP_DETAIL_HEIGHT_PX)
            )
            st.altair_chart(bar, use_container_width=True)


def _group_table(series: "pd.DataFrame", period: str) -> "pd.DataFrame":
    """Long-form category series -> one row per value: total, per_minute, trend."""
    rows = []
    minutes = period_to_minutes(period)
    for cat, g in series.groupby("category"):
        g = g.sort_values("date")
        total = int(g["count"].sum())
        label = OTHER_LABEL if cat == SENTRY_ROLLUP_RAW else cat   # "Other" -> "Others"
        rows.append({
            "category": label,
            "total": total,
            "per_minute": round(total / minutes, GROUP_ROUND_DECIMALS) if minutes else 0.0,
            "trend": g["count"].tolist(),          # the sparkline data
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values("total", ascending=False).reset_index(drop=True)


def display_group_view(field: str, label: str, value: str,
                       environment: str, period: str, base_query: str) -> None:
    """Detail sub-view for one value of the chosen dimension (e.g. one country).

    Generalises the app drill-down to any tag. "Back" returns to the Group-by
    table. All on /events-stats/ (no Discover). "Other" is the top-N rollup and
    can't be drilled; "(none)" means the tag isn't set on those errors.
    """
    if st.button("← Back to group-by"):
        st.session_state.pop("drill_group", None)
        st.session_state["group_gen"] = st.session_state.get("group_gen", 0) + 1
        st.rerun()

    st.header(f"{label} detail — {value}")

    if value == GROUP_OTHER_LABEL:
        st.info(f"“Other” is every {label.lower()} beyond the top ones combined — "
                "go back and pick a named value.")
        return
    if value == GROUP_NONE_LABEL:
        value_query = f"{base_query} !has:{field}"
    else:
        escaped = value.replace('"', '\\"')
        value_query = f'{base_query} {field}:"{escaped}"'

    over = errors_over_time(environment=environment, period=period,
                            interval=auto_interval(period), query=value_query)
    total = int(over["count"].sum()) if not over.empty else 0
    minutes = period_to_minutes(period)
    per_min = total / minutes if minutes else 0.0
    users = affected_users(environment=environment, period=period, query=value_query)

    c1, c2, c3 = st.columns(3)
    c1.metric("Total errors", f"{total:,}")
    c2.metric("Errors / min", f"{per_min:.2f}")
    c3.metric("Affected users", f"{users:,}")

    if not over.empty and total > 0:
        granularity = granularity_label(auto_interval(period))
        tooltip_fmt = "%b %d, %H:%M" if auto_interval(period) == "1h" else "%b %d, %Y"
        area = (
            alt.Chart(over)
            .mark_area(opacity=APP_DETAIL_AREA_OPACITY, line=True)
            .encode(
                x=alt.X("date:T", title=f"Time ({granularity})"),
                y=alt.Y("count:Q", title="Errors"),
                tooltip=[
                    alt.Tooltip("date:T", title="Time", format=tooltip_fmt),
                    alt.Tooltip("count:Q", title="Errors"),
                ],
            )
            .properties(height=APP_DETAIL_HEIGHT_PX)
        )
        st.altair_chart(area, use_container_width=True)

    # A secondary breakdown — by flow phase, unless we're already grouping by it.
    secondary = "payment_method" if field == "flow_phase" else "flow_phase"
    _error_breakdown(f"By {secondary.replace('_', ' ')}", secondary,
                     environment, period, value_query)


def display_group_by(environment: str, period: str, query: str) -> None:
    """User-chosen "Top by <dimension>" explorer — pick the tag to group on.

    Unlike Top apps (fixed to package_name, last hour), the dimension here is the
    user's choice and it respects the global time range. Click a row to drill in.
    """
    st.subheader("Group by")
    label = st.selectbox("Group by", list(GROUP_BY_FIELDS.keys()), key="group_by_field")
    field = GROUP_BY_FIELDS[label]

    series = category_series(field=field, environment=environment, period=period,
                             interval=auto_interval(period), query=query)
    if series.empty:
        st.info("No errors in this period.")
        return
    df = _group_table(series, period)
    if df.empty:
        st.info("No errors in this period.")
        return

    st.caption(f"Top {label.lower()} values by errors. Click a row to drill in.")
    gen = st.session_state.get("group_gen", 0)
    event = st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=f"group_by_table_{field}_{gen}",       # per-field key resets selection
        column_config={
            "category": st.column_config.TextColumn(label),
            "total": st.column_config.NumberColumn("Errors"),
            "per_minute": st.column_config.NumberColumn("Errors/min", format="%.2f"),
            "trend": st.column_config.LineChartColumn("Trend"),
        },
    )

    rows = event.selection.rows if event and event.selection else []
    if rows:
        st.session_state.drill_group = {
            "field": field, "label": label, "value": df.iloc[rows[0]]["category"],
        }
        st.rerun()


def display_top_apps(environment: str, query: str) -> None:
    """Apps with the most errors in the last hour — click a row to open its sub-view."""
    st.subheader("Top apps by errors (last hour)")
    df = top_apps(environment=environment, query=query)   # last-hour window from queries defaults
    if df.empty:
        st.info("No errors in the last hour.")
        return
    # Sentry's rollup group "Other" -> "Others" (kept in sync with the drill check).
    df = df.copy()
    df["package_name"] = df["package_name"].replace(SENTRY_ROLLUP_RAW, OTHER_LABEL)

    st.caption("Click a row to open that app's detail.")
    gen = st.session_state.get("top_apps_gen", 0)
    event = st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=f"top_apps_table_{gen}",
        column_config={
            "package_name": st.column_config.TextColumn("App"),
            "total": st.column_config.NumberColumn("Errors (1h)"),
            "per_minute": st.column_config.NumberColumn("Errors/min", format="%.2f"),
            "trend": st.column_config.LineChartColumn("Trend"),
        },
    )

    rows = event.selection.rows if event and event.selection else []
    if rows:
        # Navigate into the app sub-view (rendered by display_overview next run).
        st.session_state.drill_app = df.iloc[rows[0]]["package_name"]
        st.rerun()



def display_error_list(environment: str, period: str, query: str) -> None:
    """Paginated list of errors — ERRORS_PER_PAGE per page, click a row for detail."""
    st.subheader("Errors")
    df = top_errors(environment=environment, period=period, query=query)
    if df.empty:
        st.info("No errors.")
        return
    df = df.sort_values("count()", ascending=False).reset_index(drop=True)

    total_pages = (len(df) - 1) // ERRORS_PER_PAGE + 1
    page = st.session_state.get("error_page", 0)
    page = max(0, min(page, total_pages - 1))   # clamp if data shrank

    start = page * ERRORS_PER_PAGE
    page_df = df.iloc[start:start + ERRORS_PER_PAGE]

    st.caption("Click a row to see that error in detail.")
    gen = st.session_state.get("error_list_gen", 0)
    with st.container(border=True):
        event = st.dataframe(
            page_df,
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key=f"error_list_table_{gen}_{page}",   # per-page key: selection is page-local
            column_config={
                "error_title": st.column_config.TextColumn("Error"),
                "count()": st.column_config.NumberColumn("Count"),
            },
        )

    rows = event.selection.rows if event and event.selection else []
    if rows:
        # Open the per-error sub-view (rendered by display_overview on the next run).
        st.session_state.drill_error = page_df.iloc[rows[0]]["error_title"]
        st.rerun()

    prev_col, info_col, next_col = st.columns(PAGINATION_COL_RATIO)
    if prev_col.button("◀ Previous", disabled=(page <= 0)):
        st.session_state.error_page = page - 1
        st.rerun()
    info_col.markdown(
        f"<div style='text-align:center'>Page {page + 1} of {total_pages}</div>",
        unsafe_allow_html=True,
    )
    if next_col.button("Next ▶", disabled=(page >= total_pages - 1)):
        st.session_state.error_page = page + 1
        st.rerun()


def _error_breakdown(label: str, field: str, environment: str, period: str, query: str) -> None:
    """One horizontal bar chart: counts for `query` grouped by `field`.

    Used by the Group-by drill-down for its secondary breakdown. Uses
    category_series (/events-stats/ topEvents) so it never touches Discover.
    Silent when the tag isn't present in this slice (nothing useful to show).
    """
    series = category_series(field=field, environment=environment, period=period,
                             interval=auto_interval(period), query=query)
    if series.empty:
        return
    by_cat = series.groupby("category", as_index=False)["count"].sum()
    by_cat = by_cat[by_cat["count"] > 0]
    if by_cat.empty:
        return

    st.caption(label)
    bar = (
        alt.Chart(by_cat)
        .mark_bar()
        .encode(
            x=alt.X("count:Q", title="Errors"),
            y=alt.Y("category:N", sort="-x", title=None),
            tooltip=[
                alt.Tooltip("category:N", title=label[3:].capitalize()),
                alt.Tooltip("count:Q", title="Errors"),
            ],
        )
        .properties(height=ERROR_DETAIL_HEIGHT_PX)
    )
    st.altair_chart(bar, use_container_width=True)


def _issue_label(issue: dict) -> str:
    """One-line label for an issue in the picker (title + volume + recency)."""
    seen = (issue.get("last_seen") or "")[:16].replace("T", " ")
    return f"{issue['title']} — {issue['count']:,} events, last seen {seen}"


def _events_tag_table(events: list[dict]) -> pd.DataFrame:
    """Events -> one row each: timestamp + a column per tag (known tags first)."""
    keys = {key for ev in events for key in ev["tags"]}
    ordered = [key for key in EVENT_TAG_COLUMNS if key in keys]
    ordered += sorted(keys - set(ordered))

    rows = []
    for ev in events:
        row = {EVENT_TIME_COLUMN: pd.to_datetime(ev["timestamp"], errors="coerce", utc=True)}
        row.update({key: ev["tags"].get(key, EVENT_TAG_MISSING) for key in ordered})
        rows.append(row)
    return pd.DataFrame(rows)


def _display_issue_events(issue: dict, environment: str) -> None:
    """Per-occurrence tag values: the raw truth behind the distributions."""
    with st.expander("Recent events — the exact tag set of one occurrence"):
        try:
            events = issue_events(issue["id"], environment)
        except httpx.HTTPStatusError as exc:
            st.info(f"Couldn't load this issue's events (Sentry returned {exc.response.status_code}).")
            return
        if not events:
            st.info("Sentry has no individual events stored for this issue.")
            return

        st.caption("Click a row to see every tag on that event.")
        table = _events_tag_table(events)
        selected = st.dataframe(
            table,
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key=f"issue_events_table_{issue['id']}",
            column_config={
                EVENT_TIME_COLUMN: st.column_config.DatetimeColumn(
                    "When", format="MMM D, HH:mm:ss"
                ),
            },
        )

        rows = selected.selection.rows if selected and selected.selection else []
        if not rows:
            return
        event = events[rows[0]]
        st.markdown(f"**All tags on event `{event['event_id']}`**")
        st.dataframe(
            pd.DataFrame(sorted(event["tags"].items()), columns=["Tag", "Value"]),
            use_container_width=True,
            hide_index=True,
        )
        if event.get("sentry_url"):
            st.link_button("Open this event in Sentry ↗", event["sentry_url"])


def display_error_tags(error_title: str, environment: str, period: str,
                       error_query: str) -> None:
    """The tag VALUES behind one error — the other half of the error detail view.

    The charts above answer "how many"; this answers "with what values". It
    resolves the clicked error to the Sentry issue(s) behind it (the /issues/
    search honours our query language, unlike the project event stream) and then
    reads that issue's tag summary: every tag, its most common value, and how
    dominant that value is. Selecting a tag loads all of its values; the events
    expander shows one occurrence's complete tag set plus a link into Sentry.
    """
    st.subheader("Tag values")
    try:
        issues = issues_for_query(query=error_query, environment=environment, period=period)
    except httpx.HTTPStatusError as exc:
        st.info(
            f"Couldn't resolve this error to a Sentry issue "
            f"(Sentry returned {exc.response.status_code}), so there are no tag "
            "values to show. The charts above still apply."
        )
        return
    if not issues:
        st.info(
            "No Sentry issue matches this error in the selected range — nothing "
            "to break down by tag. Try a wider time range."
        )
        return

    issue = issues[0]
    if len(issues) > 1:
        options = issues[:ISSUE_PICKER_MAX]
        label = st.selectbox(
            f"{len(issues)} issues group under this error — pick one",
            [_issue_label(i) for i in options],
            key="error_tags_issue",
        )
        issue = next(i for i in options if _issue_label(i) == label)

    col1, col2, col3 = st.columns([1, 1, 2])
    col1.metric("Events (issue)", f"{issue['count']:,}")
    col2.metric("Affected users", f"{issue['users']:,}")
    if issue.get("sentry_url"):
        with col3:
            st.link_button("Open this issue in Sentry ↗", issue["sentry_url"])

    summary = issue_tag_summary(issue["id"], environment)
    if summary.empty:
        st.info("This issue carries no tags in this environment.")
        return

    st.caption(
        "Every tag on this error with its most common value. Share 100% means "
        "the value is constant for this error. Click a row for all of a tag's values."
    )
    selected = st.dataframe(
        summary,
        use_container_width=True,
        hide_index=True,
        height=min(TAG_SUMMARY_MAX_HEIGHT_PX, (len(summary) + 1) * 35 + 3),
        on_select="rerun",
        selection_mode="single-row",
        key=f"error_tags_table_{issue['id']}",
        column_config={
            "tag": st.column_config.TextColumn("Tag"),
            "top_value": st.column_config.TextColumn("Most common value"),
            "top_count": st.column_config.NumberColumn("Events"),
            "share": st.column_config.ProgressColumn(
                "Share", format="percent", min_value=0.0, max_value=1.0
            ),
            "events": st.column_config.NumberColumn("Tagged events"),
        },
    )

    rows = selected.selection.rows if selected and selected.selection else []
    if rows:
        tag = summary.iloc[rows[0]]["tag"]
        values = issue_tag_values(issue["id"], tag, environment)
        st.markdown(f"**All values of `{tag}`**")
        if values.empty:
            st.info("No values recorded for this tag.")
        else:
            st.dataframe(
                values,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "value": st.column_config.TextColumn("Value"),
                    "count": st.column_config.NumberColumn("Events"),
                    "share": st.column_config.ProgressColumn(
                        "Share", format="percent", min_value=0.0, max_value=1.0
                    ),
                    "last_seen": st.column_config.DatetimeColumn(
                        "Last seen", format="MMM D, HH:mm"
                    ),
                },
            )

    _display_issue_events(issue, environment)


def display_error_detail(error_title: str, environment: str, period: str, base_query: str) -> None:
    """Full sub-view for one error title, reached by clicking a row in the Errors list.

    "Back" returns to the Overview. KPIs and the trend come from /events-stats/
    (no Discover); the tag values below them come from this error's Sentry issue.
    """
    if st.button("← Back to errors"):
        st.session_state.pop("drill_error", None)
        # New table key on return so the old row selection doesn't re-navigate.
        st.session_state["error_list_gen"] = st.session_state.get("error_list_gen", 0) + 1
        st.rerun()

    st.header("Error detail")
    st.markdown(f"**`{error_title}`**")

    # Scope every widget to this one error. error_title is a Sentry tag (the
    # grouping key); escape embedded quotes so titles with quotes still match.
    escaped = error_title.replace('"', '\\"')
    error_query = f'{base_query} error_title:"{escaped}"'

    over = errors_over_time(environment=environment, period=period,
                            interval=auto_interval(period), query=error_query)
    total = int(over["count"].sum()) if not over.empty else 0
    minutes = period_to_minutes(period)
    per_min = total / minutes if minutes else 0.0
    users = affected_users(environment=environment, period=period, query=error_query)

    col1, col2, col3 = st.columns(3)
    col1.metric("Total errors", f"{total:,}")
    col2.metric("Errors / min", f"{per_min:.2f}")
    col3.metric("Affected users", f"{users:,}")

    if total == 0:
        st.info("No data for this error in the selected time range — try a wider range.")
        return

    # Trend over the selected period.
    granularity = granularity_label(auto_interval(period))
    tooltip_fmt = "%b %d, %H:%M" if auto_interval(period) == "1h" else "%b %d, %Y"
    area = (
        alt.Chart(over)
        .mark_area(opacity=APP_DETAIL_AREA_OPACITY, line=True)
        .encode(
            x=alt.X("date:T", title=f"Time ({granularity})"),
            y=alt.Y("count:Q", title="Errors"),
            tooltip=[
                alt.Tooltip("date:T", title="Time", format=tooltip_fmt),
                alt.Tooltip("count:Q", title="Errors"),
            ],
        )
        .properties(height=ERROR_DETAIL_HEIGHT_PX)
    )
    st.altair_chart(area, use_container_width=True)

    # The tag values themselves — per tag and per occurrence. They replace the
    # old per-tag bar charts: the same breakdown, with the actual values,
    # every tag instead of four, and exact counts.
    display_error_tags(error_title, environment, period, error_query)


def _sentry_error(exc: Exception) -> None:
    """Present a Sentry/network failure as a clean message instead of a traceback."""
    if isinstance(exc, SentryCircuitOpen):
        st.warning(
            f"**Sentry is getting a breather.** Too many failures in a row — "
            f"pausing requests for ~{exc.retry_in:.0f}s so we don't pile on. "
            "This screen works again automatically after that."
        )
        if st.button("↻ Retry"):
            st.rerun()
        return
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == HTTP_FORBIDDEN:
            st.error(
                "**Sentry — 403 Forbidden.** This view uses Sentry's Discover API, "
                "which this token can't access. The tag filters in the sidebar "
                "still work. Ask the Sentry admin to grant Discover access to the "
                "token."
            )
        elif code in SERVER_ERROR_STATUS:
            st.warning(
                f"**Sentry is temporarily unavailable ({code}).** It was retried "
                "automatically without luck — usually transient (load/restart on "
                "sentry02). Try again in a moment."
            )
        else:
            st.error(f"**Sentry request failed ({code}).**")
    else:
        st.warning(
            "**Couldn't reach Sentry** (network / timeout). Check the connection "
            "to sentry02 and try again."
        )
    if st.button("↻ Retry"):
        st.rerun()


def _unexpected_error(exc: Exception) -> None:
    """Any non-Sentry error: friendly message for the user, traceback hidden for devs."""
    st.error(
        "**Something went wrong.** An unexpected error stopped this view from "
        "rendering. Try again — if it keeps happening, let the team know."
    )
    with st.expander("Technical details"):
        st.code("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    if st.button("↻ Retry", key="retry_unexpected"):
        st.rerun()


def display_overview(environment: str, period: str) -> None:
    """The Overview view — filters build a query that every widget shares.

    Split into tabs so there's no long scroll; the metrics row stays on top.
    Clicking a row in Top apps opens that app's sub-view (Back returns here).
    """
    query = render_tag_filters(st.sidebar, "ov", environment, period, heading="### Filters")
    drill = st.session_state.get("drill_app")
    if drill is not None:
        display_app_view(drill, environment, period, query)
        return
    drill_err = st.session_state.get("drill_error")
    if drill_err is not None:
        display_error_detail(drill_err, environment, period, query)
        return
    drill_grp = st.session_state.get("drill_group")
    if drill_grp is not None:
        display_group_view(drill_grp["field"], drill_grp["label"], drill_grp["value"],
                           environment, period, query)
        return

    display_error_metrics(environment, period, query)   # KPI row, always on top
    display_insights_panel(environment, period, query)  # on-demand AI read
    tab_overview, tab_errors, tab_group = st.tabs(["Overview", "Errors", "Group by"])
    with tab_overview:
        bar_chart_error_counts(environment, period, query)
        display_top_apps(environment, query)
    with tab_errors:
        display_error_list(environment, period, query)
    with tab_group:
        display_group_by(environment, period, query)


def render_health_banner(environment: str) -> None:
    """Top-of-page heartbeat: warn if Sentry has gone silent (possibly down).

    Dead-man's-switch — alerts on the *absence* of the expected background error
    rate. On any query failure it stays quiet: a hard-down Sentry is already
    surfaced by the view's error handler, so this only adds the "silent" case.
    """
    try:
        health = sentry_health(environment=environment)
    except Exception:
        return
    silent = health["hours_silent"]
    baseline = health["baseline_per_hour"]
    if silent >= SILENCE_THRESHOLD_HOURS and baseline * silent >= HEALTH_MIN_EXPECTED:
        st.warning(
            f"⚠️ **No errors received in the last ~{silent}h** — normally about "
            f"{baseline:.0f}/h. Sentry ({environment}) may be down or not ingesting "
            "events, or the app stopped sending them. Worth checking sentry02."
        )


def main() -> None:
    """Main function to run the Streamlit app."""
    configure_page()
    st.title("Error Dashboard")

    # Global controls — defined once, in the sidebar.
    view = st.sidebar.radio("View", VIEWS)
    environment = st.sidebar.selectbox("Environment", ENVIRONMENTS)
    period = st.sidebar.selectbox("Time range", PERIODS, index=PERIODS.index("30d"))

    render_health_banner(environment)   # heartbeat warning, above every view

    # A Sentry / network failure surfaces as a readable message, not a traceback.
    try:
        if view == "Error rates":
            display_error_rates(environment, period)
        else:
            display_overview(environment, period)
    except (SentryCircuitOpen, httpx.HTTPStatusError, httpx.RequestError) as exc:
        _sentry_error(exc)
    except Exception as exc:                       # any code bug → friendly + hidden traceback
        _unexpected_error(exc)

if __name__ == "__main__":
    main()
