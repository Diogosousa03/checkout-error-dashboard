"""Streamlit app — entry point and the Overview view.

  - Overview: metrics, error counts over time, top apps, error list (filterable)
  - Error rates: lives in error_rates.py; imported and rendered here.

Global controls: view, environment (production/development), time range.
The tag filters are shown only in the Overview view.
Run with:  uv run streamlit run src/error_dashboard/app.py
"""

import traceback

import altair as alt
import httpx
import streamlit as st

from error_dashboard.queries import (
    affected_users,
    category_series,
    errors_over_time,
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
SYNTHETIC_APPS = {"Other", "(no app tag)"}   # rows that aren't a real package_name

# --- Per-error drill-down (click a row in the Errors list) ---
ERROR_DETAIL_HEIGHT_PX = 180
# The tags this one error is broken down by, in the detail sub-view.
ERROR_DETAIL_FIELDS = {
    "By flow phase":     "flow_phase",
    "By severity":       "level",
    "By payment method": "payment_method",
    "By country":        "geo_country",
}

# --- HTTP status handling ---
HTTP_FORBIDDEN = 403
SERVER_ERROR_STATUS = (500, 502, 503, 504)


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

    if app == "Other":
        st.info("“Other” is every app beyond the top 5 combined — go back and pick a named app.")
        return
    if app == "(no app tag)":
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


def display_top_apps(environment: str, query: str) -> None:
    """Apps with the most errors in the last hour — click a row to open its sub-view."""
    st.subheader("Top apps by errors (last hour)")
    df = top_apps(environment=environment, query=query)   # last-hour window from queries defaults
    if df.empty:
        st.info("No errors in the last hour.")
        return

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
    """One horizontal bar chart: this error's counts grouped by `field`.

    Uses category_series (/events-stats/ topEvents) so it never touches Discover.
    Silent when the tag isn't present on this error (nothing useful to show).
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


def display_error_detail(error_title: str, environment: str, period: str, base_query: str) -> None:
    """Full sub-view for one error title, reached by clicking a row in the Errors list.

    "Back" returns to the Overview. Everything here is on /events-stats/ (no
    Discover): KPIs, the trend over the period, and breakdowns by the tags that
    matter for a checkout error (phase, severity, payment method, country).
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

    # Trend over the selected period.
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
            .properties(height=ERROR_DETAIL_HEIGHT_PX)
        )
        st.altair_chart(area, use_container_width=True)

    # Breakdowns by the tags that characterise a checkout error.
    for label, field in ERROR_DETAIL_FIELDS.items():
        _error_breakdown(label, field, environment, period, error_query)



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

    display_error_metrics(environment, period, query)   # KPI row, always on top
    tab_overview, tab_errors = st.tabs(["Overview", "Errors"])
    with tab_overview:
        bar_chart_error_counts(environment, period, query)
        display_top_apps(environment, query)
    with tab_errors:
        display_error_list(environment, period, query)


def main() -> None:
    """Main function to run the Streamlit app."""
    configure_page()
    st.title("Error Dashboard")

    # Global controls — defined once, in the sidebar.
    view = st.sidebar.radio("View", VIEWS)
    environment = st.sidebar.selectbox("Environment", ENVIRONMENTS)
    period = st.sidebar.selectbox("Time range", PERIODS, index=PERIODS.index("30d"))

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
