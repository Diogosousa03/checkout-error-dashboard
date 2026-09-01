"""Helper functions: period/granularity math and the shared Sentry GET.

The period/granularity helpers are pure (no I/O); `sentry_get` is the one shared
transport helper the client layer routes every request through. It also guards
Sentry with a retry, a circuit breaker, and 429 (rate-limit) handling.
"""

import time

import httpx
import structlog

log = structlog.get_logger()

# Timeout (seconds) applied to every Sentry API request.
SENTRY_TIMEOUT = 30.0

# Transient failures worth retrying: server-side 5xx (client 4xx like 403/404 are
# not — retrying them just wastes time). Timeouts / transport errors retry too.
SENTRY_RETRIES = 2             # extra attempts after the first
SENTRY_RETRY_BACKOFF = 0.8     # seconds; grows each attempt
_RETRY_STATUS = {500, 502, 503, 504}

# Circuit breaker: after this many consecutive *availability* failures, stop
# calling Sentry for a cooldown window so button-mashing during an outage can't
# pile on. (4xx like 403 do NOT count — they're not availability problems.)
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_COOLDOWN_SECONDS = 30

# On HTTP 429 (rate limited), obey Retry-After but never sleep longer than this.
RETRY_AFTER_CAP_SECONDS = 10

# How long cached Sentry results stay fresh (Streamlit re-runs on every click).
CACHE_TTL_SECONDS = 300

# Time-unit conversions, so period/interval math reads without bare literals.
MINUTES_PER_HOUR = 60
HOURS_PER_DAY = 24
DAYS_PER_WEEK = 7

# Bucket size auto-picked per period, so the chart always stays readable.
PERIOD_INTERVAL = {"7d": "1h", "30d": "1d", "90d": "1d"}
GRANULARITY_LABEL = {"1h": "hourly", "1d": "daily", "1w": "weekly"}

# Minutes in each time unit, built from the conversions above.
_MINUTES_PER_UNIT = {
    "m": 1,
    "h": MINUTES_PER_HOUR,
    "d": MINUTES_PER_HOUR * HOURS_PER_DAY,
    "w": MINUTES_PER_HOUR * HOURS_PER_DAY * DAYS_PER_WEEK,
}

# Process-global breaker state (shared across reruns/sessions → it protects the
# API globally, not just per user). Simple by design; approximate is fine here.
_breaker = {"failures": 0, "open_until": 0.0}


class SentryCircuitOpen(Exception):
    """Raised when the breaker is open — no request is made at all."""

    def __init__(self, retry_in: float):
        self.retry_in = retry_in
        super().__init__(f"Sentry circuit open; retry in {retry_in:.0f}s")


def _circuit_remaining() -> float | None:
    """Seconds left if the breaker is open, else None."""
    remaining = _breaker["open_until"] - time.monotonic()
    return remaining if remaining > 0 else None


def _record_success() -> None:
    """A good response clears the breaker."""
    _breaker["failures"] = 0
    _breaker["open_until"] = 0.0


def _record_failure() -> None:
    """An availability failure; open the breaker once they pile up."""
    _breaker["failures"] += 1
    if _breaker["failures"] >= CIRCUIT_FAILURE_THRESHOLD:
        _breaker["open_until"] = time.monotonic() + CIRCUIT_COOLDOWN_SECONDS


def _retry_after_seconds(response) -> float:
    """Seconds to wait from a 429's Retry-After header (capped).

    Falls back to the normal backoff if the header is missing or an HTTP-date
    (the date form is not parsed here — the fallback keeps it simple and safe).
    """
    header = response.headers.get("Retry-After")
    if header:
        try:
            return min(float(header), RETRY_AFTER_CAP_SECONDS)
        except ValueError:
            pass
    return SENTRY_RETRY_BACKOFF


def sentry_get(url, params, headers, log_event, **log_fields):
    """GET the Sentry API: circuit-breaker guard, retry transient failures, honor 429.

    The one place HTTP happens for the whole app. Callers pass the endpoint URL,
    query params, auth headers, a structlog event name, and any structured log
    fields; they call `.json()` on the returned response.
    """
    # A — if the breaker is open, fail fast WITHOUT touching Sentry.
    remaining = _circuit_remaining()
    if remaining is not None:
        raise SentryCircuitOpen(remaining)

    for attempt in range(SENTRY_RETRIES + 1):
        last = attempt == SENTRY_RETRIES
        log.info(log_event, attempt=attempt, **log_fields)
        try:
            resp = httpx.get(url, headers=headers, params=params, timeout=SENTRY_TIMEOUT)
            resp.raise_for_status()
            _record_success()                      # reset the breaker on any success
            return resp
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 429:                      # B — rate limited: obey Retry-After
                if last:
                    _record_failure()
                    raise
                time.sleep(_retry_after_seconds(exc.response))
                continue
            if status in _RETRY_STATUS:            # transient 5xx → retry
                if last:
                    _record_failure()
                    raise
                # fall through to the backoff sleep and loop again
            else:
                raise                              # 4xx (403/404): not availability,
                                                   # re-raise without tripping breaker
        except (httpx.TimeoutException, httpx.TransportError):
            if last:
                _record_failure()
                raise
        time.sleep(SENTRY_RETRY_BACKOFF * (attempt + 1))


def period_to_minutes(period: str) -> int:
    """Convert a period string like '7d' / '1h' / '2w' into minutes."""
    value, unit = int(period[:-1]), period[-1]
    return value * _MINUTES_PER_UNIT[unit]


def interval_to_minutes(interval: str) -> int:
    """Convert a bucket interval like '5m' / '1h' / '1d' / '1w' into minutes.

    Used to turn a bucket's raw error count into a rate (errors per minute).
    """
    value, unit = int(interval[:-1]), interval[-1]
    return value * _MINUTES_PER_UNIT[unit]


def auto_interval(period: str) -> str:
    """Pick a bucket size that keeps the chart readable for the given period."""
    return PERIOD_INTERVAL.get(period, "1d")


def granularity_label(interval: str) -> str:
    """Human-readable name for a bucket interval, e.g. '1d' -> 'daily'."""
    return GRANULARITY_LABEL.get(interval, interval)
