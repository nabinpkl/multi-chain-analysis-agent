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

    Raises TimeoutError if the span doesn't appear within
    `timeout_s`. The default (30s) is comfortably above the OTel
    SDK BatchSpanProcessor delay (5s) plus the otel-collector
    batch interval, with margin for slow CH inserts. Increase
    via the timeout_s arg for unusually large turns.
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
