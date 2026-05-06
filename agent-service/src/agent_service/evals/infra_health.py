"""Detect terminal provider failures on a trace.

A "terminal provider failure" is when the agent could not complete
its turn because of an upstream issue (OpenRouter returned a
malformed response that even a retry couldn't fix, network died
mid-call, etc), as opposed to the agent completing its turn with
behavior that probes evaluate against. The two look identical at
the probe layer (downstream probes simply fail because the spans
they assert on never emitted), so without an infra-health signal
every provider flake registers as a regression in the baseline
diff.

The detector reads two pydantic_ai-emitted span attributes:
- The `agent run` span carries an OTel `StatusCode=ERROR` plus a
  `StatusMessage` containing the exception class name + message
  when the agent loop terminated with an unhandled exception. This
  is the strongest signal: pydantic_ai's own loop gave up.
- A `chat <model>` span with the same ERROR/StatusMessage shape
  indicates the LLM call itself errored. After the layer-1 retry
  wrapper, a single chat error followed by a successful retry is
  a healthy outcome (we recovered); only when the parent `agent
  run` also errored does the trace count as terminal.

We treat "any agent run with StatusCode=ERROR" as the canonical
terminal-failure signal. This deliberately does NOT include
`mcae.primitive.*` errors, because primitive errors are part of
the normal agent flow (the agent reads them as `<external_data>`
and decides what to do); they are NOT provider-side failures.
"""

from __future__ import annotations

from agent_service.evals.ch import ClickHouseClient


async def has_terminal_provider_failure(
    trace_id: str, ch: ClickHouseClient
) -> tuple[bool, str | None]:
    """Returns (is_terminal_failure, summary). Summary is a short
    human-readable string for the operator's triage path
    ("UnexpectedModelBehavior on agent run") when terminal,
    None when the trace looks healthy.

    The query reads OTel `StatusCode` (set by pydantic_ai's
    instrumentation when the span's context manager exits with an
    exception). StatusMessage carries the exception class name
    and short message.
    """
    rows = await ch.query(
        "SELECT StatusMessage AS msg "
        "FROM otel.otel_traces "
        "WHERE TraceId = {tid:String} "
        "AND SpanName = 'agent run' "
        "AND StatusCode = 'STATUS_CODE_ERROR' "
        "ORDER BY Timestamp ASC "
        "LIMIT 1",
        tid=trace_id,
    )
    if not rows:
        return False, None
    msg = rows[0]["msg"] or ""
    # Trim to the first line of the StatusMessage; pydantic-ai's
    # stack traces can run hundreds of chars, but the first line is
    # the exception class + message that the operator needs.
    summary = msg.split("\n", 1)[0][:200]
    return True, summary
