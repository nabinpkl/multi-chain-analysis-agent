"""Role-agnostic output channel for the agent core.

The core never knows whether its emissions land on an SSE stream, a
Kafka alerts topic, an in-process return value (peer-consult), or
some future transport. It sees a `TurnSink` and calls `emit(event,
msg)` on it; the sink does whatever its driver needs.

Today there's exactly one sink implementation, `SseSink`, used by the
chat driver. New drivers ship a new sink alongside their envelope
builder; the core stays unchanged.

Why a `Protocol` and not an abstract base class:

* No inheritance constraint on driver-side sink classes. A driver can
  use any object that satisfies `async def emit(event, msg)`,
  including a closure-bound callable wrapped behind a tiny adapter.
* Mirrors how MCP tool servers and A2A agents in the mid-2026 spec
  expose duck-typed transports  the core's contract is the method
  signature, not a class hierarchy.

Why `emit` takes `(event: str, msg)` rather than a single typed proto:

* Frontend SSE consumers dispatch on the `event` name (Claim,
  Narrative, Progress, …). Sink impls that don't speak SSE (Kafka,
  in-process) still benefit from the discriminator: a Kafka sink
  routes by event name to a per-frame topic; an in-process sink
  filters by event name to surface only the final Narrative.
* `msg` stays untyped here for the same reason as `history` in the
  envelope: the foundation avoids pulling proto-generated types into
  its public surface beyond what's strictly required. Sinks know the
  proto family their driver speaks; the protocol does not.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Protocol

from google.protobuf import json_format


class TurnSink(Protocol):
    """Role-specific frame consumer. Drivers implement transport.

    Protocol means duck-typed: any class with a matching `async def
    emit(event, msg)` satisfies the contract, no inheritance required.
    """

    async def emit(self, event: str, msg: Any) -> None:
        """Receive one frame. Implementation decides what to do with it.

        For SSE chat: serialize msg to canonical proto-JSON, build a
        `{event, data}` dict, push onto a queue the chat driver drains
        as the response stream.

        For Kafka monitor (future): serialize msg, publish to the
        per-event topic.

        For in-process peer-consult (future): hold the final Narrative
        for direct return; ignore intermediate events or buffer them
        for trace export.
        """
        ...


class SseSink:
    """Chat-driver sink. Buffers `{event, data}` dicts onto an internal
    asyncio queue; the chat driver iterates the queue with `frames()`
    and yields each dict to FastAPI's `StreamingResponse`.

    The buffering shape is required because the current
    `loop_driver.run_turn` is itself an async generator yielding
    SSE-shaped dicts. Once the loop body moves into `core.run_one_turn`
    (the follow-on commit), the body will `await sink.emit(...)` at
    each emission point and the chat driver will drain the queue
    instead of yielding directly.

    Why a queue rather than a list: the loop body and the chat
    driver's response generator run concurrently; the queue gives the
    driver something to await on, so a slow consumer doesn't force
    the loop body to buffer an unbounded list in memory.
    """

    # Sentinel pushed by `close()` so `frames()` knows the producer is
    # done. A bare `None` would conflict with a None payload from a
    # future caller; a unique object never round-trips through emit().
    _SENTINEL: Any = object()

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, str] | object] = asyncio.Queue()

    async def emit(self, event: str, msg: Any) -> None:
        """Serialize `msg` to canonical proto-JSON and queue an SSE
        frame dict matching the existing `_frame()` shape in
        `loop_driver`. `preserving_proto_field_name=False` keeps the
        camelCase wire format the frontend expects (per
        `AGENTS.md` "wire format per hop").
        """
        data = json_format.MessageToJson(
            msg, preserving_proto_field_name=False, indent=None
        )
        await self._queue.put({"event": event, "data": data})

    async def close(self) -> None:
        """Signal end-of-stream so `frames()` exits its loop."""
        await self._queue.put(self._SENTINEL)

    async def frames(self) -> AsyncIterator[dict[str, str]]:
        """Drain queued frames in order. Exits when `close()` has been
        called and the queue is empty."""
        while True:
            item = await self._queue.get()
            if item is self._SENTINEL:
                return
            assert isinstance(item, dict)
            yield item
