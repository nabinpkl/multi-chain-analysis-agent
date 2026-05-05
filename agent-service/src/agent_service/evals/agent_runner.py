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


async def invoke_agent_get_trace_id(
    inputs: dict,
    *,
    base_url: str,
    http: httpx.AsyncClient,
) -> AgentRun:
    """POST `inputs` to /agent/ask, follow the SSE stream until the
    AgentDone frame arrives, return the trace id.

    `inputs` is mutated to set `runType=eval` if not already
    present. Production traffic doesn't set runType; eval-driven
    traffic does. The mutation is intentional; if a YAML case
    happens to set its own runType (e.g. "dev" for a debugging
    suite), the case wins.
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
            # AgentDone is the only frame that carries traceId.
            if "traceId" in frame and "elapsedMs" in frame:
                return AgentRun(
                    trace_id=frame["traceId"],
                    session_id=session_id,
                    elapsed_ms=int(frame["elapsedMs"]),
                    started_at=started,
                    finished_at=datetime.now(timezone.utc),
                )

    raise RuntimeError(
        f"agent stream for session {session_id} closed without "
        "an AgentDone frame; no trace id captured"
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
