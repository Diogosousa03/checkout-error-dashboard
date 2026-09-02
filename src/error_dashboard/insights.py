"""LLM insights layer — a short, grounded read of the current error state.

Turns the aggregates the dashboard *already computed* into 3–5 concise, factual
insights plus one recommended action, generated on demand by Claude.

Golden rule (Week9 spec): the model only ever sees aggregates — counts, rates,
percentages. It never sees a raw Sentry payload and never any user identifier
(no user.id, no wallet address). build_facts() reads only aggregate query
outputs, so nothing that identifies a user can reach the external API.

Layout:
  build_facts()       — pure-ish (only the dashboard's own queries), no LLM.
  generate_insights() — the one Claude call, structured via tool use, cached.
"""

import json

import streamlit as st

from error_dashboard.queries import (
    affected_users,
    category_series,
    errors_over_time,
    rate_summary,
    top_errors,
)
from error_dashboard.settings import settings
from error_dashboard.utils import CACHE_TTL_SECONDS, auto_interval, period_to_minutes

# --- Model & call config ---
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1024
INSIGHTS_TIMEOUT_SECONDS = 30.0

# --- Facts payload shaping ---
SAMPLE_RATE = 0.5          # Sentry sampleRate — counts are ~half of reality.
TOP_CODES_LIMIT = 8        # how many error titles to hand the model
BREAKDOWN_LIMIT = 6        # rows kept per tag breakdown (flow_phase, payment_method)
PCT_DECIMALS = 1


class InsightsUnavailable(Exception):
    """Config problem the user can fix (missing API key / SDK not installed)."""


# --------------------------------------------------------------------------- #
#  build_facts — assemble a small aggregates-only JSON object                  #
# --------------------------------------------------------------------------- #

def _breakdown(field: str, environment: str, period: str, query: str) -> list[dict]:
    """Aggregate one tag into [{value, errors, pct}], biggest first. Resilient."""
    try:
        series = category_series(
            field=field, environment=environment, period=period,
            interval=auto_interval(period), query=query,
        )
    except Exception:
        return []
    if series.empty:
        return []
    grouped = series.groupby("category", as_index=False)["count"].sum()
    total = int(grouped["count"].sum())
    if total <= 0:
        return []
    grouped = grouped.sort_values("count", ascending=False).head(BREAKDOWN_LIMIT)
    return [
        {
            "value": str(row["category"]),
            "errors": int(row["count"]),
            "pct": round(row["count"] / total * 100, PCT_DECIMALS),
        }
        for _, row in grouped.iterrows()
    ]


def build_facts(environment: str, period: str, query: str) -> dict:
    """Assemble the aggregates-only facts payload for the current view.

    Every section is wrapped so one failing query (e.g. Discover 403 on the error
    list) degrades that section to empty instead of breaking the whole payload.
    Nothing here carries a user identifier — only counts, rates and percentages.
    """
    # Totals — from the same events-stats series the Overview metrics use.
    try:
        over = errors_over_time(
            environment=environment, period=period,
            interval=auto_interval(period), query=query,
        )
        total_errors = int(over["count"].sum()) if not over.empty else 0
    except Exception:
        total_errors = 0
    minutes = period_to_minutes(period)
    per_min = round(total_errors / minutes, 2) if minutes else 0.0

    try:
        users = affected_users(environment=environment, period=period, query=query)
    except Exception:
        users = None

    # Trend — current vs previous period (respects the active filters).
    try:
        summ = rate_summary(environment=environment, period=period, query=query)
        trend = {
            "current_total": summ["curr_total"],
            "previous_total": summ["prev_total"],
            "delta_pct": round(summ["delta_pct"], PCT_DECIMALS)
            if summ["delta_pct"] is not None else None,
        }
    except Exception:
        trend = None

    # Top error titles (uses Discover /events/ — may 403; then just omitted).
    try:
        te = top_errors(environment=environment, period=period, query=query)
        if not te.empty:
            te = te.sort_values("count()", ascending=False).head(TOP_CODES_LIMIT)
            top_codes = [
                {"error_title": str(r["error_title"]), "errors": int(r["count()"])}
                for _, r in te.iterrows()
            ]
        else:
            top_codes = []
    except Exception:
        top_codes = []

    return {
        "context": {
            "environment": environment,
            "period": period,
            "active_filters": query,
        },
        "totals": {
            "errors": total_errors,
            "errors_per_min": per_min,
            "affected_users": users,
        },
        "trend": trend,
        "by_flow_phase": _breakdown("flow_phase", environment, period, query),
        "by_payment_method": _breakdown("payment_method", environment, period, query),
        "top_codes": top_codes,
        "sampling": {
            "rate": SAMPLE_RATE,
            "note": "Counts are sampled at ~50%; magnitudes are approximate but "
                    "the shape (proportions, trend) is unbiased.",
        },
    }


# --------------------------------------------------------------------------- #
#  generate_insights — the single Claude call (structured via tool use)        #
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = (
    "You are an SRE assistant reading aggregated checkout-error metrics for a "
    "payments flow. From ONLY the numbers provided, produce 3–5 short, factual "
    "insights about the current state and exactly one recommended next action.\n\n"
    "Rules:\n"
    "- Use only the figures in the payload. Never invent error codes, causes, or "
    "numbers that aren't present.\n"
    "- When you infer a cause, mark that insight kind='hypothesis'; a plain "
    "observation of the numbers is kind='fact'.\n"
    "- Counts are sampled (~50%): describe magnitudes as approximate, never exact.\n"
    "- Prioritise what matters operationally — the authorization phase, fatal/"
    "error severity, a sharp trend, or a dominant payment method or code.\n"
    "- Keep each insight to one sentence. Return your answer only through the "
    "emit_insights tool."
)

INSIGHTS_TOOL = {
    "name": "emit_insights",
    "description": "Return the structured insights for the current dashboard state.",
    "input_schema": {
        "type": "object",
        "properties": {
            "insights": {
                "type": "array",
                "minItems": 3,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "One-sentence insight."},
                        "kind": {"type": "string", "enum": ["fact", "hypothesis"]},
                        "severity_hint": {
                            "type": "string",
                            "enum": ["info", "warning", "critical"],
                        },
                    },
                    "required": ["text", "kind", "severity_hint"],
                },
            },
            "recommended_action": {"type": "string"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "caveats": {"type": "string"},
        },
        "required": ["insights", "recommended_action", "confidence", "caveats"],
    },
}


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def generate_insights(facts_json: str) -> dict:
    """Ask Claude for structured insights about `facts_json`.

    Cached by the exact facts string, so identical states and re-renders don't
    re-call the API. Raises InsightsUnavailable for fixable config problems
    (missing key / SDK); other failures propagate for the caller to message.
    """
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise InsightsUnavailable(
            "The `anthropic` package isn't installed. Run `uv add anthropic`."
        ) from exc

    key = getattr(settings, "anthropic_api_key", None)
    if not key:
        raise InsightsUnavailable(
            "No ANTHROPIC_API_KEY set. Add it to your .env to enable insights."
        )

    client = Anthropic(api_key=key, timeout=INSIGHTS_TIMEOUT_SECONDS)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        tools=[INSIGHTS_TOOL],
        tool_choice={"type": "tool", "name": "emit_insights"},
        messages=[{"role": "user", "content": facts_json}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "emit_insights":
            return dict(block.input)
    raise RuntimeError("The model did not return structured insights.")
