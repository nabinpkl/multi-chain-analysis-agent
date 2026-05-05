"""Invokes the production agent over HTTP and captures the OTel
trace id of the resulting turn.

Eval-driven invocation goes through the same `/agent/ask` +
`/agent/stream/{session_id}` path that production users hit. The
runner sets `runType="eval"` on every request so the resulting
`mcae.turn` span carries `mcae.run.type=eval`, letting analytics
queries discriminate eval volume from real-user traffic in the
shared `otel.otel_traces` table.

The trace id is delivered by the agent service inside the
`AgentDone` SSE frame (added in Ship 1 of agent-observability,
ADR 13). We iterate the SSE stream until that frame arrives.

After AgentDone arrives, the trace is NOT yet guaranteed to be
queryable in ClickHouse. The OTel BatchSpanProcessor buffers
spans before export, and the otel-collector batches before
writing to CH. `wait_for_trace_indexed` polls until the
`mcae.turn` root span (which closes last in a turn, after every
child span has flushed) appears, with a timeout.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from agent_service.evals.ch import ClickHouseClient


@dataclass(frozen=True, slots=True)
class AgentRun:
    """One agent invocation's outcome from the eval runner's
    perspective. The runner doesn't care about the agent's narrative
    output; the probes do that work later by querying CH for the
    trace by id.
    """

    trace_id: str
    session_id: str
    elapsed_ms: int
    started_at: datetime
    finished_at: datetime


async def _consume_until_done(
    stream_resp: httpx.Response, session_id: str
) -> dict:
    """Iterate the SSE stream until the AgentDone frame arrives.
    Returns the frame dict. Caller is responsible for the timeout
    bound around this coroutine."""
    async for line in stream_resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        payload = line.removeprefix("data:").strip()
        if not payload:
            continue
        try:
            frame = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if "traceId" in frame and "elapsedMs" in frame:
            return frame

    raise RuntimeError(
        f"agent stream for session {session_id} closed without "
        "an AgentDone frame; no trace id captured"
    )


async def invoke_agent_get_trace_id(
    inputs: dict,
    *,
    base_url: str,
    http: httpx.AsyncClient,
    stream_timeout_s: float = 180.0,
) -> AgentRun:
    """POST `inputs` to /agent/ask, follow the SSE stream until the
    AgentDone frame arrives, return the trace id.

    `inputs` is shallow-copied with `runType=eval` set if not
    already present. Production traffic doesn't set runType;
    eval-driven traffic does. If a YAML case happens to set its own
    runType (e.g. "dev" for a debugging suite), the case wins.

    `stream_timeout_s` caps the wall-clock duration of a single
    turn's SSE stream. httpx's `timeout` setting governs initial
    connect/read; once bytes are flowing it does not enforce a
    total stream duration. Without this cap a hung agent (e.g.
    constitution gate stalled on a throttled provider, see
    issue #16) would hang the eval CLI indefinitely. The default
    180s is generous for a normal turn (~25s today) and tight
    enough that a stuck turn fails loudly within minutes.

    Raises RuntimeError on empty traceId (the agent ran without
    OTel emission, e.g. `OTEL_SDK_DISABLED=true`); polling for a
    nonexistent trace would waste 30s on the wait_for_trace_indexed
    timeout and report the wrong root cause.
    """
    inputs = {**inputs}
    inputs.setdefault("runType", "eval")
    started = datetime.now(timezone.utc)

    ask_resp = await http.post(f"{base_url}/agent/ask", json=inputs)
    ask_resp.raise_for_status()
    session_id = ask_resp.json()["sessionId"]

    stream_url = f"{base_url}/agent/stream/{session_id}"
    async with http.stream("GET", stream_url) as stream_resp:
        stream_resp.raise_for_status()
        try:
            frame = await asyncio.wait_for(
                _consume_until_done(stream_resp, session_id),
                timeout=stream_timeout_s,
            )
        except asyncio.TimeoutError as e:
            raise TimeoutError(
                f"agent stream for session {session_id} did not emit "
                f"AgentDone within {stream_timeout_s:.0f}s; the agent "
                "is likely stuck (provider throttling? gate hang?). "
                "Increase stream_timeout_s if this is a known long turn."
            ) from e

    trace_id = frame["traceId"]
    if not trace_id:
        raise RuntimeError(
            f"agent for session {session_id} returned an empty traceId; "
            "OTel emission is disabled or broken. Eval cannot probe a "
            "trace that does not exist; check OTEL_SDK_DISABLED and "
            "the otel-collector connection."
        )
    return AgentRun(
        trace_id=trace_id,
        session_id=session_id,
        elapsed_ms=int(frame["elapsedMs"]),
        started_at=started,
        finished_at=datetime.now(timezone.utc),
    )


async def wait_for_trace_indexed(
    trace_id: str,
    ch: ClickHouseClient,
    *,
    timeout_s: float = 30.0,
    poll_interval_s: float = 0.5,
) -> None:
    """Block until the `mcae.turn` root span for `trace_id` is
    queryable in `otel.otel_traces`. The root span closes after
    every child has flushed, so its presence is the proxy for
    "the whole trace is exported."

    Why polling and not an event:

    The agent process, the otel-collector, and ClickHouse are
    three independent processes coordinating asynchronously. There
    is no shared synchronous primitive that says "the trace is
    visible to SELECT now":

    - ClickHouse has no insert-event API. LISTEN/NOTIFY does not
      exist; LIVE VIEW / WATCH was deprecated. CH is built for
      batched analytics, not event-driven coordination.
    - The OTel SDK's force_flush() drains the agent-side buffer to
      the collector but does not propagate through the collector's
      own batch processor (which exists for CH's sake: 1-row
      INSERTs are ~100x slower than batched ones and create part-
      churn that triggers merge backpressure).
    - The collector publishes nothing back to the agent or eval
      caller when its batch flushes downstream.

    So the consumer asks. That is polling, by definition.

    The cost is negligible: each poll is a single point query on
    an indexed TraceId column, sub-millisecond CH-side. Sixty polls
    spread over 30 seconds is well below noise compared to the
    agent-run cost of the turn itself.

    Do NOT replace this with: a tighter collector batch (breaks
    production write throughput), force_flush in the agent (fixes
    only one of three buffering layers), Kafka fan-out (real
    infra cost for no real win at our scale), or a separate eval
    pipeline (over-engineered against a non-problem). The polling
    is structurally correct.

    Raises TimeoutError if the span doesn't appear within
    `timeout_s`.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        rows = await ch.query(
            "SELECT count() AS n FROM otel.otel_traces "
            "WHERE TraceId = {tid:String} AND SpanName = 'mcae.turn'",
            tid=trace_id,
        )
        if rows and int(rows[0]["n"]) > 0:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(
                f"trace {trace_id} did not appear in otel.otel_traces "
                f"within {timeout_s:.0f}s; the root mcae.turn span was "
                "not exported. Check otel-collector logs."
            )
        await asyncio.sleep(poll_interval_s)
