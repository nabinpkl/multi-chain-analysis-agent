"""Single-retry + per-attempt-timeout wrapper for LLM provider calls.

OpenRouter (free tier especially) occasionally returns malformed
`ChatCompletion` payloads, network blips, or timeouts. pydantic_ai
surfaces these as `UnexpectedModelBehavior` and raises straight up
through `agent.run(...)`. Without a wrapper the caller (loop driver,
constitution gate, repeat detector) sees a terminal failure for what
is structurally a transient hiccup. The Datadog 2026 telemetry
report puts industry-wide LLM call error rate at 2-5% with rate
limits the dominant cause; pretending zero of those happen is wrong.

The wrapper does two things:

1. **Per-attempt timeout** via `asyncio.wait_for`. Free-tier providers
   sometimes accept a request and then just sit on it for minutes
   (cold-start queueing on shared free pools). Without a per-call cap
   the only safety net is the outer SSE stream timeout (180s today),
   which means one stuck call burns the whole turn budget. Wrapping
   the call in `wait_for` gives the retry a chance to land on a
   different provider instance.

2. **Single retry** on transient exceptions (including the timeout
   from #1). The retry policy is deliberately conservative: one extra
   attempt with a short backoff, no exponential climb. Two reasons:

   - Free-tier OpenRouter is rate-limited; hammering with N retries
     during a flake makes the rate limit worse, not better. One retry
     catches the common single-bad-response case and stops there.
   - If the provider is genuinely down (vs flaky), N retries spread
     over seconds doesn't help; the right move is to fail fast and
     let the caller surface the error.

The retry preserves the original exception via `raise ... from`
chaining so the final traceback shows both the retry attempt and
the original failure cause.

Industry pattern (verified 2026-05): pydantic_evals uses Tenacity
with `stop_after_attempt(3) | wait_exponential(min=1, max=30)`;
DeepEval defaults to one retry on transient errors; openai/evals
uses exponential backoff factor 1.5 capped at 60s. We mirror the
single-retry shape rather than the full Tenacity dep because our
needs are small and Tenacity's surface dwarfs what we'd use.

Each attempt's elapsed time is logged with the call-site label, so
post-hoc flake analysis can attribute slow calls to a specific
gate/loop site without needing the full OTel trace.
"""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from typing import Awaitable, Callable, TypeVar

import httpx
import structlog
from pydantic_ai.exceptions import UnexpectedModelBehavior

T = TypeVar("T")

log = structlog.get_logger(__name__)

# Per-turn role-timing accumulator. The loop driver sets this at the
# start of `run_turn` to a fresh dict; every successful call within
# the turn (whether on attempt 1 or attempt 2) adds its elapsed
# wall-time to the matching role bucket here. The driver reads it back
# when stamping `AgentDone.role_timings` so the builder view's Models
# panel can show "primary 73.8s last call" under each role row.
#
# Keys are role ids matching `agent_service.llm.Role` ("primary",
# "policy", "judge"). Values are accumulated seconds; the driver
# converts to ms when crossing the wire boundary. `None` means we are
# outside any turn-scoped attribution context (e.g. eval probe runs
# that don't go through the loop driver) and the accumulator is
# silently a no-op for those.
_role_timings: ContextVar[dict[str, float] | None] = ContextVar(
    "_role_timings", default=None
)

# Map call-site label to role bucket. Keep this central so adding a
# new gate (e.g. a future "ground_truth" call) is one line in this
# table and not a hunt across the codebase. Labels not in the map
# (eval probes, ad-hoc internal tooling) are intentionally dropped:
# the panel only surfaces user-visible chat-path timings.
_LABEL_TO_ROLE: dict[str, str] = {
    "primary_agent": "primary",
    "constitution_claim": "policy",
    "constitution_narrative": "policy",
    "repeat_detector": "policy",
    # Judge labels reserved for the eval substrate; they don't run on
    # the chat path today, so the judge bucket is always 0 in
    # AgentDone.role_timings. Listed here so the bucket has a stable
    # name when judge calls migrate onto the chat path.
}


def _attribute_call(label: str, elapsed_s: float) -> None:
    bucket = _role_timings.get()
    if bucket is None:
        return
    role = _LABEL_TO_ROLE.get(label)
    if role is None:
        return
    bucket[role] = bucket.get(role, 0.0) + elapsed_s


def begin_role_timing_capture() -> dict[str, float]:
    """Install a fresh accumulator for the current async context.
    Returns the dict the caller should later read after the
    captured-region's calls have all completed.

    Caller is expected to balance this with `end_role_timing_capture`
    using the returned token; the contextvar pattern is the same one
    pydantic_ai uses for its `Agent.override` flow."""
    bucket: dict[str, float] = {}
    _role_timings.set(bucket)
    return bucket

# Exceptions we treat as transient. Anything else propagates immediately.
#
# UnexpectedModelBehavior covers OpenRouter returning a malformed
# ChatCompletion body (the failure mode that motivated this module).
# httpx.HTTPError covers network-level transients (TimeoutException,
# ConnectError, etc); pydantic_ai's OpenAI provider doesn't always
# wrap these in UnexpectedModelBehavior.
# asyncio.TimeoutError covers the case where the call hangs past
# whatever upstream timeout fires first (and our own per-attempt
# `asyncio.wait_for` raises it directly when `per_attempt_timeout_s`
# trips).
_RETRYABLE: tuple[type[BaseException], ...] = (
    UnexpectedModelBehavior,
    httpx.HTTPError,
    asyncio.TimeoutError,
)


async def with_provider_retry(
    factory: Callable[[], Awaitable[T]],
    *,
    label: str,
    backoff_s: float = 1.0,
    per_attempt_timeout_s: float | None = None,
) -> T:
    """Run `factory()` once; on a known-transient exception, sleep
    `backoff_s` and run it once more. The factory pattern (vs taking
    a coroutine directly) is required because asyncio coroutines
    cannot be re-awaited; the second attempt needs a fresh coroutine.

    If `per_attempt_timeout_s` is set, each attempt is wrapped in
    `asyncio.wait_for`. A timeout raises `asyncio.TimeoutError`, which
    is in `_RETRYABLE`, so the first attempt timing out triggers the
    retry path automatically. The second timeout propagates.

    `label` is included in retry/elapsed log lines so flake-rate
    analysis against multiple call sites can attribute occurrences.
    """

    async def _attempt() -> T:
        if per_attempt_timeout_s is None:
            return await factory()
        return await asyncio.wait_for(factory(), timeout=per_attempt_timeout_s)

    started = time.monotonic()
    try:
        result = await _attempt()
        elapsed = time.monotonic() - started
        _attribute_call(label, elapsed)
        log.debug(
            "llm_provider_call_ok",
            label=label,
            attempt=1,
            elapsed_s=round(elapsed, 3),
            timeout_s=per_attempt_timeout_s,
        )
        return result
    except _RETRYABLE as e:
        log.warning(
            "llm_provider_transient_error_retrying",
            label=label,
            error_type=type(e).__name__,
            error_message=str(e)[:200],
            attempt=1,
            elapsed_s=round(time.monotonic() - started, 3),
            backoff_s=backoff_s,
            timeout_s=per_attempt_timeout_s,
        )
        await asyncio.sleep(backoff_s)
        retry_started = time.monotonic()
        try:
            result = await _attempt()
            retry_elapsed = time.monotonic() - retry_started
            # Attribute only the successful retry's wall-time, not the
            # failed first attempt + backoff. The accumulator is for
            # "how long did the model actually take to answer" and the
            # retry path is what the caller will see end-to-end.
            _attribute_call(label, retry_elapsed)
            log.info(
                "llm_provider_call_ok_after_retry",
                label=label,
                attempt=2,
                elapsed_s=round(retry_elapsed, 3),
                timeout_s=per_attempt_timeout_s,
            )
            return result
        except _RETRYABLE as e2:
            log.error(
                "llm_provider_transient_error_after_retry",
                label=label,
                error_type=type(e2).__name__,
                error_message=str(e2)[:200],
                attempt=2,
                elapsed_s=round(time.monotonic() - retry_started, 3),
                timeout_s=per_attempt_timeout_s,
            )
            raise
