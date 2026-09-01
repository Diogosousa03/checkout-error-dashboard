"""Error-rates view — the second screen of the dashboard.

Every rate representation on one screen: current rate + trend, rate over time,
and rate broken down by a tag (with a clickable name list that drills into one
category). All built on /events-stats/ (no Discover), like the queries it uses.
`app.py` just imports and calls display_error_rates().
"""

import altair as alt
import streamlit as st
import pandas as pd

from error_dashboard.queries import category_series, errors_over_time, rate_summary
from error_dashboard.utils import (
    auto_interval,
    granularity_label,
    interval_to_minutes,
    period_to_minutes,
)

# Tags offered as the "break down by" dimension in this view.
CATEGORY_FIELDS = {"Flow phase": "flow_phase", "Payment method": "payment_method"}

# --- Chart geometry (pixels) & styling ---
RATE_CHART_HEIGHT_PX = 220
SPARKLINE_HEIGHT_PX = 60
LINE_CHART_HEIGHT_PX = 240
DETAIL_CHART_HEIGHT_PX = 180
LEGEND_ROW_HEIGHT_PX = 24          # per name in the clickable legend (tight spacing)
LEGEND_FONT_SIZE = 13

# --- Opacities ---
FULL_OPACITY = 1.0
AREA_FILL_OPACITY = 0.35
DETAIL_FILL_OPACITY = 0.4
LEGEND_DIM_OPACITY = 0.45          # unselected names in the legend
DIMMED_LINE_OPACITY = 0.12         # unselected lines when one is picked

# --- Layout & scaling ---
CHART_LEGEND_COL_RATIO = [5, 1]    # rate-by-category: chart vs name list
PERCENT_SCALE = 100                # fraction -> percentage


def _rate_over_time(environment: str, period: str) -> None:
    """Errors per minute over the period (rate line chart)."""
    st.subheader("Rate over time")
    interval = auto_interval(period)
    granularity = granularity_label(interval)
    minutes = interval_to_minutes(interval)

    df = errors_over_time(environment=environment, period=period, interval=interval)
    if df.empty:
        st.info("No errors in this period.")
        return
    df = df.copy()
    df["rate"] = df["count"] / minutes   # count per bucket -> errors per minute

    tooltip_fmt = "%b %d, %H:%M" if interval == "1h" else "%b %d, %Y"
    chart = (
        alt.Chart(df)
        .mark_area(opacity=AREA_FILL_OPACITY, line=True)
        .encode(
            x=alt.X("date:T", title=f"Time ({granularity})"),
            y=alt.Y("rate:Q", title="Errors / min"),
            tooltip=[
                alt.Tooltip("date:T", title="Time", format=tooltip_fmt),
                alt.Tooltip("rate:Q", title="Errors/min", format=".2f"),
                alt.Tooltip("count:Q", title="Errors (bucket)"),
            ],
        )
        .properties(height=RATE_CHART_HEIGHT_PX)
    )
    st.altair_chart(chart, use_container_width=True)
    st.caption(f"Rate = errors per {granularity[:-2] if granularity.endswith('ly') else granularity} bucket ÷ {minutes} min.")


def _current_rate_and_trend(environment: str, period: str) -> None:
    """Headline current errors/min with a comparison vs the previous period."""
    st.subheader("Current rate & trend")
    summ = rate_summary(environment=environment, period=period)

    col1, col2, col3 = st.columns(3)
    delta = f"{summ['delta_pct']:+.0f}% vs previous" if summ["delta_pct"] is not None else None
    # delta_color "inverse": more errors (positive) shows red, fewer shows green.
    col1.metric("Errors / min (current)", f"{summ['curr_per_min']:.2f}",
                delta=delta, delta_color="inverse")
    col2.metric("Total (current period)", f"{summ['curr_total']:,}")
    col3.metric("Total (previous period)", f"{summ['prev_total']:,}")

    current = summ["current"]
    if not current.empty:
        spark = (
            alt.Chart(current)
            .mark_area(opacity=AREA_FILL_OPACITY, line=True)
            .encode(
                x=alt.X("date:T", axis=None),
                y=alt.Y("count:Q", axis=None),
                tooltip=[alt.Tooltip("date:T", title="Time"), alt.Tooltip("count:Q", title="Errors")],
            )
            .properties(height=SPARKLINE_HEIGHT_PX)
        )
        st.altair_chart(spark, use_container_width=True)


def _category_legend(cats: list[str], color_scale, field: str) -> str | None:
    """Clickable name list (right column). Returns the selected category or None.

    Each name is a text mark, so clicking it IS reported through on_select — a
    bind="legend" selection updates the chart visually but never reaches Python.
    """
    legend_df = pd.DataFrame({"category": cats})
    legpick = alt.selection_point(fields=["category"], on="click", name="legpick")
    name_list = (
        alt.Chart(legend_df)
        .mark_text(align="left", fontSize=LEGEND_FONT_SIZE, fontWeight="bold")
        .add_params(legpick)
        .encode(
            y=alt.Y("category:N", axis=None, sort=cats),
            text="category:N",
            color=alt.Color("category:N", scale=color_scale, legend=None),
            opacity=alt.condition(legpick, alt.value(FULL_OPACITY), alt.value(LEGEND_DIM_OPACITY)),
        )
        .properties(height=LEGEND_ROW_HEIGHT_PX * len(cats))
    )
    event = st.altair_chart(name_list, use_container_width=True,
                            on_select="rerun", key=f"legend_{field}")
    state = event.selection if event else None
    picked = (state or {}).get("legpick", [])
    return picked[0]["category"] if picked and "category" in picked[0] else None


def _category_lines(df, color_scale, selected, label, granularity, tooltip_fmt) -> None:
    """Per-category rate lines; the selected line stays opaque, the rest dim.

    Highlight is driven from Python via an opacity column, so it doesn't depend
    on any cross-chart Vega wiring.
    """
    df = df.copy()
    if selected is None:
        df["_op"] = FULL_OPACITY
    else:
        df["_op"] = df["category"].map(lambda c: FULL_OPACITY if c == selected else DIMMED_LINE_OPACITY)
    lines = (
        alt.Chart(df)
        .mark_line()
        .encode(
            x=alt.X("date:T", title=f"Time ({granularity})"),
            y=alt.Y("rate:Q", title="Errors / min"),
            color=alt.Color("category:N", scale=color_scale, legend=None),
            opacity=alt.Opacity("_op:Q", scale=None),
            tooltip=[
                alt.Tooltip("category:N", title=label),
                alt.Tooltip("date:T", title="Time", format=tooltip_fmt),
                alt.Tooltip("rate:Q", title="Errors/min", format=".2f"),
            ],
        )
        .properties(height=LINE_CHART_HEIGHT_PX)
    )
    st.altair_chart(lines, use_container_width=True)


def _category_detail(df, selected, grand, label, period, granularity, tooltip_fmt) -> None:
    """Detail panel for the selected category — sliced from the in-memory data."""
    cat_df = df[df["category"] == selected]
    total = int(cat_df["count"].sum())
    per_min = total / period_to_minutes(period)
    share = (total / grand * PERCENT_SCALE) if grand else 0.0

    st.markdown(f"**Selected {label.lower()}: `{selected}`**")
    c1, c2, c3 = st.columns(3)
    c1.metric("Errors", f"{total:,}")
    c2.metric("Errors / min", f"{per_min:.2f}")
    c3.metric("Share of errors", f"{share:.1f}%")

    detail = (
        alt.Chart(cat_df)
        .mark_area(opacity=DETAIL_FILL_OPACITY, line=True)
        .encode(
            x=alt.X("date:T", title=f"Time ({granularity})"),
            y=alt.Y("rate:Q", title="Errors / min"),
            tooltip=[
                alt.Tooltip("date:T", title="Time", format=tooltip_fmt),
                alt.Tooltip("rate:Q", title="Errors/min", format=".2f"),
            ],
        )
        .properties(height=DETAIL_CHART_HEIGHT_PX)
    )
    st.altair_chart(detail, use_container_width=True)


def _rate_by_category(environment: str, period: str) -> None:
    """Rate broken down by a tag — composes the legend, lines and detail.

    Click a category name in the list on the right to highlight its line and open
    a detail panel — built from the data already loaded (no extra Sentry call).
    """
    st.subheader("Rate by category")
    label = st.selectbox("Break down by", list(CATEGORY_FIELDS.keys()))
    field = CATEGORY_FIELDS[label]

    interval = auto_interval(period)
    granularity = granularity_label(interval)
    minutes = interval_to_minutes(interval)

    df = category_series(field=field, environment=environment, period=period, interval=interval)
    if df.empty:
        st.info("No errors in this period.")
        return
    df = df.copy()
    df["rate"] = df["count"] / minutes

    grand = int(df["count"].sum())
    cats = sorted(df["category"].unique())
    color_scale = alt.Scale(domain=cats)          # shared so line & name colours match
    tooltip_fmt = "%b %d, %H:%M" if interval == "1h" else "%b %d, %Y"

    st.caption(f"Click a {label.lower()} name on the right to highlight its line.")
    chart_col, legend_col = st.columns(CHART_LEGEND_COL_RATIO)
    with legend_col:
        selected = _category_legend(cats, color_scale, field)
    with chart_col:
        _category_lines(df, color_scale, selected, label, granularity, tooltip_fmt)

    if selected is not None:
        _category_detail(df, selected, grand, label, period, granularity, tooltip_fmt)


def display_error_rates(environment: str, period: str) -> None:
    """The Error rates view — current rate on top, the rest split into tabs."""
    st.header("Error rates")
    _current_rate_and_trend(environment, period)   # headline, always on top
    tab_time, tab_category = st.tabs(["Rate over time", "Rate by category"])
    with tab_time:
        _rate_over_time(environment, period)
    with tab_category:
        _rate_by_category(environment, period)
