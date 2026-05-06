"""Single-retry wrapper for LLM provider transient failures.

OpenRouter (free tier especially) occasionally returns malformed
`ChatCompletion` payloads, network blips, or timeouts. pydantic_ai
surfaces these as `UnexpectedModelBehavior` and raises straight up
through `agent.run(...)`. Without a wrapper the caller (loop driver,
constitution gate, repeat detector) sees a terminal failure for what
is structurally a transient hiccup. The Datadog 2026 telemetry
report puts industry-wide LLM call error rate at 2-5% with rate
limits the dominant cause; pretending zero of those happen is wrong.

This module wraps `agent.run(...)` calls with a single retry. The
retry policy is deliberately conservative: one extra attempt with a
short backoff, no exponential climb. Two reasons:

1. Free-tier OpenRouter is rate-limited; hammering with N retries
   during a flake makes the rate limit worse, not better. One retry
   catches the common single-bad-response case and stops there.

2. If the provider is genuinely down (vs flaky), N retries spread
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
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

import httpx
import structlog
from pydantic_ai.exceptions import UnexpectedModelBehavior

T = TypeVar("T")

log = structlog.get_logger(__name__)

# Exceptions we treat as transient. Anything else propagates immediately.
#
# UnexpectedModelBehavior covers OpenRouter returning a malformed
# ChatCompletion body (the failure mode that motivated this module).
# httpx.HTTPError covers network-level transients (TimeoutException,
# ConnectError, etc); pydantic_ai's OpenAI provider doesn't always
# wrap these in UnexpectedModelBehavior.
# asyncio.TimeoutError covers the case where the call hangs past
# whatever upstream timeout fires first.
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
) -> T:
    """Run `factory()` once; on a known-transient exception, sleep
    `backoff_s` and run it once more. The factory pattern (vs taking
    a coroutine directly) is required because asyncio coroutines
    cannot be re-awaited; the second attempt needs a fresh coroutine.

    `label` is included in retry log lines so flake-rate analysis
    against multiple call sites can attribute occurrences.
    """
    try:
        return await factory()
    except _RETRYABLE as e:
        log.warning(
            "llm_provider_transient_error_retrying",
            label=label,
            error_type=type(e).__name__,
            error_message=str(e)[:200],
            backoff_s=backoff_s,
        )
        await asyncio.sleep(backoff_s)
        try:
            return await factory()
        except _RETRYABLE as e2:
            log.error(
                "llm_provider_transient_error_after_retry",
                label=label,
                error_type=type(e2).__name__,
                error_message=str(e2)[:200],
            )
            raise
