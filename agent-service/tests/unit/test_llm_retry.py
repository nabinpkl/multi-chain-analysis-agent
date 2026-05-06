"""Tests for the provider-call retry helper.

Single-retry semantics: succeed-on-first-try is one call, succeed-
on-retry is two calls, fail-twice raises, non-retryable propagates
immediately. The factory pattern (vs taking a coroutine) is
load-bearing because asyncio coroutines cannot be re-awaited;
these tests exercise that constraint.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior

from agent_service.llm_retry import with_provider_retry


@pytest.mark.asyncio
async def test_succeeds_first_try_calls_factory_once() -> None:
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    result = await with_provider_retry(factory, label="t", backoff_s=0)
    assert result == "ok"
    assert calls == 1


@pytest.mark.asyncio
async def test_retries_once_on_unexpected_model_behavior() -> None:
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise UnexpectedModelBehavior("openrouter returned None")
        return "ok"

    result = await with_provider_retry(factory, label="t", backoff_s=0)
    assert result == "ok"
    assert calls == 2


@pytest.mark.asyncio
async def test_retries_once_on_httpx_error() -> None:
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("transient")
        return "ok"

    result = await with_provider_retry(factory, label="t", backoff_s=0)
    assert result == "ok"
    assert calls == 2


@pytest.mark.asyncio
async def test_raises_after_second_failure() -> None:
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        raise UnexpectedModelBehavior("persistent")

    with pytest.raises(UnexpectedModelBehavior):
        await with_provider_retry(factory, label="t", backoff_s=0)
    assert calls == 2


@pytest.mark.asyncio
async def test_non_retryable_propagates_immediately() -> None:
    """ValueError isn't in the retryable tuple; one call only, no
    backoff, original exception bubbles. Catches the failure mode
    where someone widens the retryable set without thinking."""
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("logic bug")

    with pytest.raises(ValueError):
        await with_provider_retry(factory, label="t", backoff_s=0)
    assert calls == 1


@pytest.mark.asyncio
async def test_per_attempt_timeout_triggers_retry() -> None:
    """A first-attempt hang past `per_attempt_timeout_s` raises
    `asyncio.TimeoutError` (in `_RETRYABLE`), which trips the retry.
    Second attempt completes fast; final result is the success value.
    Pins the documented "stuck call gets a second chance" behavior."""
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(10)  # blow past the timeout
            return "never"
        return "ok"

    result = await with_provider_retry(
        factory, label="t", backoff_s=0, per_attempt_timeout_s=0.05
    )
    assert result == "ok"
    assert calls == 2


@pytest.mark.asyncio
async def test_per_attempt_timeout_raises_after_two_timeouts() -> None:
    """If both attempts time out, the final asyncio.TimeoutError
    propagates. Caller sees a clean failure, not a hang."""
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(10)
        return "never"

    with pytest.raises(asyncio.TimeoutError):
        await with_provider_retry(
            factory, label="t", backoff_s=0, per_attempt_timeout_s=0.05
        )
    assert calls == 2


@pytest.mark.asyncio
async def test_no_timeout_when_per_attempt_timeout_s_is_none() -> None:
    """Backward compat: callers that don't pass `per_attempt_timeout_s`
    get the un-timed-out behavior (a long sleep completes normally).
    Catches the regression where the default flips to a finite value
    and silently breaks existing call sites."""
    async def factory() -> str:
        # 0.1s is fine; the test would hang only if a default timeout
        # were ALSO 0.1s, which it isn't (None).
        await asyncio.sleep(0.1)
        return "ok"

    result = await with_provider_retry(factory, label="t", backoff_s=0)
    assert result == "ok"


@pytest.mark.asyncio
async def test_factory_pattern_supports_re_invocation() -> None:
    """Confirms we accept a callable factory not a pre-built coroutine.
    A pre-built coroutine could not be re-awaited; this test catches
    the regression where someone refactors to take a coroutine
    directly and the second attempt RuntimeErrors."""
    factory_invocations = 0

    async def factory() -> str:
        nonlocal factory_invocations
        factory_invocations += 1
        if factory_invocations == 1:
            raise UnexpectedModelBehavior("first")
        return "ok"

    result = await with_provider_retry(factory, label="t", backoff_s=0)
    assert result == "ok"
    assert factory_invocations == 2
