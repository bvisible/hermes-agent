"""Retry utilities — jittered backoff for decorrelated retries.

Jittered delays (vs. fixed exponential) prevent thundering-herd retry spikes
when many sessions hit the same rate-limited provider concurrently.
"""

import random
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

# Monotonic counter for jitter-seed uniqueness within a process; locked
# because concurrent gateway sessions retry simultaneously.
_jitter_counter = 0
_jitter_lock = threading.Lock()

# Z.AI Coding Plan's GLM-5.2 endpoint often returns 429 code 1305 ("service may be
# temporarily overloaded"). Short retries hammer the same window, so after
# ``_ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS`` normal retries the wait widens progressively;
# the cap stays interactive-friendly (a TUI message should fail visibly in minutes).
# The short count is shared by ``adaptive_rate_limit_backoff`` and
# ``zai_coding_overload_retry_ceiling`` so the two cannot silently desync.
_ZAI_CODING_OVERLOAD_LONG_BACKOFF = (30.0, 60.0, 90.0, 120.0)
_ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS = 3


def parse_retry_after_seconds(value_or_headers: Any) -> Optional[float]:
    """Parse a ``Retry-After`` value (numeric / HTTP-date) or a headers mapping (both casings tried) into
    seconds, clamped at 0.0; None when absent / unparseable."""
    raw = value_or_headers
    if raw is not None and not isinstance(raw, (str, int, float)):
        getter = getattr(raw, "get", None)
        if not callable(getter):
            return None
        try:
            raw = getter("Retry-After")
            if raw is None:
                raw = getter("retry-after")
        except Exception:
            return None
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return max(0.0, float(raw))
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        pass
    # HTTP-date form (RFC 7231): seconds until that instant, clamped at 0.
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:  # older stdlib returns None instead of raising
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


# Free-text "reset" grammars providers put in error bodies, tried in order. One table so the
# conversation loop's error context and the credential pool's cooldown agree on the same wait.
_QUOTA_RESET_DELAY_RE = re.compile(r"quotaResetDelay[:\s\"]+(\d+(?:\.\d+)?)(ms|s)", re.IGNORECASE)
# "Resets in 4hr 5min" (weekly usage limits), "resets in 2 hours 5 minutes", "resets in 30s".
_RESETS_IN_RE = re.compile(
    r"resets?\s+in\s+"
    r"(?:(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b\s*)?"
    r"(?:(\d+(?:\.\d+)?)\s*(?:m|min|mins|minute|minutes)\b\s*)?"
    r"(?:(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)\b)?", re.IGNORECASE,
)
_RETRY_AFTER_SECONDS_RE = re.compile(r"retry\s+(?:after\s+)?(\d+(?:\.\d+)?)\s*(?:sec|secs|seconds|s\b)", re.IGNORECASE)


def _quota_reset_seconds(m: "re.Match[str]") -> float:
    value = float(m.group(1))
    return value / 1000.0 if m.group(2).lower() == "ms" else value


def _resets_in_seconds(m: "re.Match[str]") -> Optional[float]:
    if not any(m.groups()):  # "resets in" with no unit-bearing number: not this grammar
        return None
    return float(m.group(1) or 0) * 3600 + float(m.group(2) or 0) * 60 + float(m.group(3) or 0)


# An explicit "retry after N s" wins over "resets in ..." (the credential pool's precedence):
# a body carrying both describes a short throttle inside a long quota window, and the
# shorter explicit wait is the one the provider actually asks for.
RETRY_DELAY_PATTERNS = (
    (_QUOTA_RESET_DELAY_RE, _quota_reset_seconds),
    (_RETRY_AFTER_SECONDS_RE, lambda m: float(m.group(1))),
    (_RESETS_IN_RE, _resets_in_seconds),
)


def reset_delay_from_message(message: str) -> Optional[float]:
    """Seconds-until-reset parsed from free-text provider error messages, or None."""
    if not message:
        return None
    for pattern, to_seconds in RETRY_DELAY_PATTERNS:
        m = pattern.search(message)
        if m and (seconds := to_seconds(m)) is not None:
            return seconds
    return None


def jittered_backoff(attempt: int, *, base_delay: float = 5.0, max_delay: float = 120.0, jitter_ratio: float = 0.5) -> float:
    """min(base * 2^(attempt-1), max_delay) + uniform jitter in
    [0, jitter_ratio * delay]. ``attempt`` is 1-based."""
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter

    exponent = max(0, attempt - 1)
    delay = max_delay if (exponent >= 63 or base_delay <= 0) else min(base_delay * (2 ** exponent), max_delay)

    # Seed from time + counter so coarse clocks still decorrelate.
    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    return delay + random.Random(seed).uniform(0, jitter_ratio * delay)


def _error_text(error: Any) -> str:
    """Best-effort flattened provider error text for retry classification."""
    parts = [error, getattr(error, "message", None), getattr(error, "body", None), getattr(error, "response", None)]
    return " ".join(str(part) for part in parts if part is not None).lower()


def is_zai_coding_overload_error(*, base_url: str | None, model: str | None, error: Any) -> bool:
    """True only for the narrow Z.AI Coding Plan overload shape (429 + code
    1305 / "temporarily overloaded"), so ordinary quota 429s still fail fast."""
    text = _error_text(error)
    return (
        getattr(error, "status_code", None) == 429
        and "api.z.ai/api/coding/paas/v4" in (base_url or "").lower()
        and "glm-5.2" in (model or "").lower()
        and ("1305" in text or "temporarily overloaded" in text)
    )


def adaptive_rate_limit_backoff(
    attempt: int, *, base_url: str | None, model: str | None, error: Any, default_wait: float,
    short_attempts: int = _ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS,
) -> tuple[float, str | None]:
    """``(wait_seconds, reason_label)``: ``default_wait`` for most providers; Z.AI Coding GLM-5.2 overloads keep
    ``short_attempts`` short retries, then 30→60→90→120s with light jitter. ``attempt`` is 1-based."""
    if not is_zai_coding_overload_error(base_url=base_url, model=model, error=error):
        return default_wait, None
    if attempt <= short_attempts:
        return default_wait, "zai_coding_overload_short"
    idx = min(attempt - short_attempts - 1, len(_ZAI_CODING_OVERLOAD_LONG_BACKOFF) - 1)
    base_delay = _ZAI_CODING_OVERLOAD_LONG_BACKOFF[idx]
    return jittered_backoff(1, base_delay=base_delay, max_delay=base_delay, jitter_ratio=0.2), "zai_coding_overload_long"


# //// Neoffice — added (no upstream equivalent, 20.09). A provider that ANSWERS
# //// "come back in 5s" is telling us it is alive and busy. A provider that is simply
# //// broken tells us nothing. Upstream spends the same three-strike budget on both,
# //// and that is what kills our turns: measured on osiris over every agent log since
# //// 17.08, the express lane turned calls away often enough to kill **84 real user
# //// turns** — 23 on the desk, 61 across the poles (compta 31, projet 14, ventes 8,
# //// rh 5, support 3) — with the Retry-After honoured every single time. Three
# //// attempts are only TWO waits, so the real patience was 10 seconds.
# ////
# //// That 84 is a CORRECTED figure and the correction is worth carrying: the first
# //// count said 196, until the dead sessions were crossed against state.db and 113 of
# //// them turned out to be our own cache-warmup timer, not people. A number that
# //// counts sessions without asking what they were is not a measurement.
# ////
# //// The shape is upstream's own, one function below: detect the announced condition,
# //// raise the loop ceiling for THAT error only. What differs is the unit — a budget
# //// in SECONDS, not in attempts — because the wait is set by the provider, not by us:
# //// the same budget buys nine tries when it says 5s and two when it says 20s, which
# //// is the right behaviour in both cases and needs no second knob.
# ////
# //// Deliberately OFF by default (budget 0.0 = today's behaviour, unchanged). The
# //// number to set comes from the lane's own occupancy, measured on the provider's
# //// side over 14 days and 114 876 chat calls: median 9s, p90 22, p95 31, max 97 —
# //// so 30s of patience covers 95 % of episodes and 45s covers 99 %.
# ////
# //// An earlier hazard fit derived from OUR logs is deliberately not quoted here. It
# //// was computed on turn counts that included the cache-warmup timer, which loops on
# //// a 120s expiry and has nothing like a user turn's law; and the lane itself is
# //// moving under it — the provider raised its concurrent-long-job limit from 2 to 5,
# //// and that timer is being retired. A projection built on two contaminated points,
# //// against a distribution that is about to shift, is worth less than the direct
# //// measurement above. Set the budget from that, or from a fresh one.
_ANNOUNCED_WAIT_TYPES = ("service_unavailable", "upstream_unavailable")


def announced_wait_kind(error: Any) -> Optional[str]:
    """The provider's own name for an announced, self-resolving unavailability.

    Read from the error body (``error.type``, or the top level), never guessed from a
    status code: a 503 alone does not say whether the far side is busy or broken.
    """
    body = getattr(error, "body", None)
    if not isinstance(body, dict):
        return None
    nested = body.get("error")
    payload = nested if isinstance(nested, dict) else body
    kind = payload.get("type")
    return kind if kind in _ANNOUNCED_WAIT_TYPES else None


def announced_wait_retry_ceiling(budget_seconds: float, announced_wait: float,
                                 floor: int = 3, *, retry_count: int = 0,
                                 already_spent: float = 0.0) -> int:
    """Loop ceiling granting ONE more attempt while the budget still covers the wait.

    Rewritten the day the provider started computing its own ``Retry-After`` from the
    shortest remaining long job, bounded to [2, 30]s — observed 10s, then 4, then 6 in
    one saturated episode. The first version divided the budget by the FIRST announced
    wait and returned a fixed ceiling, which is wrong the moment the second differs.
    Seconds are what the budget is denominated in, so seconds are what must be counted.

    ``already_spent`` is the announced time this turn has consumed so far; the caller
    accumulates it. ``floor`` keeps this from ever REDUCING a ceiling another rule set.
    Grants ``retry_count + 2`` because the loop gives up when ``retry_count >= ceiling``
    BEFORE computing that attempt's backoff — one past the attempt being decided.
    """
    if budget_seconds <= 0 or announced_wait <= 0:
        return floor
    if already_spent + announced_wait > budget_seconds:
        return floor
    return max(floor, retry_count + 2)


def announced_wait_spend(already_spent: float, announced_wait: float,
                         budget_seconds: float) -> float:
    """The running tally after this wait, or ``already_spent`` when it would overrun.

    Separate from the ceiling so the CALLER cannot forget to advance it: counting only
    the attempts that were widened left the first waits free, and a 30s budget slept 40.
    """
    if budget_seconds <= 0 or announced_wait <= 0:
        return already_spent
    if already_spent + announced_wait > budget_seconds:
        return already_spent
    return already_spent + announced_wait


def zai_coding_overload_retry_ceiling(short_attempts: int = _ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS) -> int:
    """Retry-loop ceiling for the full Z.AI overload schedule: one past the last long entry,
    because the loop gives up when ``retry_count >= ceiling`` BEFORE computing the attempt's
    backoff (the default ``api_max_retries`` of 3 equals ``short_attempts``)."""
    return short_attempts + len(_ZAI_CODING_OVERLOAD_LONG_BACKOFF) + 1
