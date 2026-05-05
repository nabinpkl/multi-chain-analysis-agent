"""HTTP client wrapping the Rust data plane on :8002.

Stage 2 of the proto migration. Every request and response on this
boundary is **binary protobuf** (`Content-Type: application/x-protobuf`)
per the AGENTS.md "Wire format per hop" matrix: service-to-service
hops use binary, browser hops use JSON. The Rust side accepts JSON as
a curl-debuggable fallback; production traffic from this client is
always binary.

All proto types come from `multichain.wire.shared.v1` (auto-generated
by `just regen-wire-types` from `proto/multichain/wire/shared/v1/`).
Do NOT hand-write parallel pydantic shapes here. If a new type is
needed, add it to the `.proto` source and re-run codegen.

Snapshot lease usage:

    lease = await client.begin_turn()
    try:
        out = await client.wallet_profile(addr=..., snapshot_id=lease.snapshot_id)
    finally:
        await client.end_turn(lease.snapshot_id)
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import httpx
import structlog
from google.protobuf import json_format
from google.protobuf.message import Message
from opentelemetry import trace

from multichain.wire.shared.v1 import (
    community_summary_pb2 as cs_pb,
    primitive_envelope_pb2 as env_pb,
    scope_pb2 as scope_pb,
    snapshot_pb2 as snap_pb,
    wallet_profile_pb2 as wp_pb,
)

from . import spans

# Module-level tracer. Honours whatever provider init_otel() registered;
# resolves to a no-op tracer in tests where OTEL_SDK_DISABLED=true so
# `with tracer.start_as_current_span(...)` stays cheap.
_tracer = trace.get_tracer(__name__)


def _digest12(body: bytes) -> str:
    """sha256 of the response body, truncated to 12 hex chars. Used as
    `primitive.output_digest` so two replays of the same primitive are
    visibly identical in trace output without bloating span attrs with
    the full payload (which can be 50KB of subgraph data)."""
    return hashlib.sha256(body).hexdigest()[:12]

log = structlog.get_logger(__name__)

CT_PROTOBUF = "application/x-protobuf"
HEADERS_PROTO = {
    "Content-Type": CT_PROTOBUF,
    "Accept": CT_PROTOBUF,
}


# ---------------------------------------------------------------------------
# Public DTO surface. Stays small: callers receive the typed proto
# message directly for outputs (provenance is a `RepeatedCompositeContainer`
# of `ProvenanceRef`, value is a parsed Python dict from the envelope's
# google.protobuf.Struct), and a single `SnapshotLease` value object for
# the begin/end lifecycle.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SnapshotLease:
    snapshot_id: str
    expires_at_ms: int
    window_secs: int


@dataclass(frozen=True, slots=True)
class PrimitiveResult:
    """Decoded `PrimitiveResponseEnvelope`.

    `value` is a plain Python dict materialized from the envelope's
    `google.protobuf.Struct` field. The Rust side fills it by serializing
    the typed primitive output (e.g. `WalletProfileOutput`) through
    serde_json into the Struct shape; on this side we read it back as a
    dict so existing callers can still address fields by name. This is a
    deliberate v0 shortcut. A follow-up replaces the generic envelope
    with per-primitive typed responses (oneof or per-route messages).

    `provenance` stays typed: a list of proto `ProvenanceRef` messages.
    """

    value: dict
    provenance: list  # list[provenance_pb2.ProvenanceRef]
    subgraph_slice: object | None  # Optional[subgraph_pb2.SubgraphSlice]


class PrimitiveError(Exception):
    """Mirror of Rust `PrimitiveError`. `kind` matches the JSON body's
    `kind` field returned by `error_response()` on the Rust side. The
    Rust boundary keeps errors JSON-shaped on both binary and JSON
    paths for client compat (no proto `Status` message defined yet).

    Special case: `kind == "snapshot_gone"` (HTTP 410) means the
    snapshot the Python side held expired (GC swept it, or Rust
    restarted). Caller should `/turn/begin` a fresh snapshot and retry.
    """

    def __init__(self, kind: str, message: str, status_code: int):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_time_scope_live() -> scope_pb.TimeScope:
    """Construct `TimeScope` with the Live oneof case set. Stage 2 only
    exposes Live; Range arms route through stub primitives that already
    refuse upstream."""
    ts = scope_pb.TimeScope()
    ts.live.CopyFrom(scope_pb.LiveScope())
    return ts


def _make_time_scope_range(from_s: int, to_s: int) -> scope_pb.TimeScope:
    ts = scope_pb.TimeScope()
    ts.range.CopyFrom(scope_pb.RangeScope(from_s=from_s, to_s=to_s))
    return ts


def _decode_envelope(body: bytes) -> PrimitiveResult:
    env = env_pb.PrimitiveResponseEnvelope()
    env.ParseFromString(body)
    # Convert google.protobuf.Struct -> Python dict. preserving_proto_field_name
    # keeps snake_case so existing callers that index `value["total_volume_lamports"]`
    # keep working (the Struct on the Rust side is filled from serde_json output
    # which uses the source-of-truth proto field names).
    value_dict = json_format.MessageToDict(env.value, preserving_proto_field_name=True)
    subgraph = env.subgraph_slice if env.HasField("subgraph_slice") else None
    return PrimitiveResult(
        value=value_dict,
        provenance=list(env.provenance),
        subgraph_slice=subgraph,
    )


def _raise_from_error(resp: httpx.Response) -> None:
    """Errors come back as JSON regardless of request format (per the
    Rust boundary in api/primitives.rs). Parse the standard
    `{"error","kind"}` shape."""
    try:
        payload = resp.json()
        kind = str(payload.get("kind", "internal"))
        message = str(payload.get("error", resp.text))
    except Exception:
        kind = "internal"
        message = resp.text
    log.warning(
        "primitive_call_failed",
        status=resp.status_code,
        kind=kind,
        message=message,
    )
    raise PrimitiveError(kind=kind, message=message, status_code=resp.status_code)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class PrimitiveClient:
    """Async client over the Rust data plane. Caller owns the
    lifecycle: build once at app startup, close on shutdown."""

    def __init__(self, base_url: str, timeout_s: float = 30.0):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_s),
        )

    async def close(self) -> None:
        await self._client.aclose()

    # -----------------------------------------------------------------
    # Snapshot lease
    # -----------------------------------------------------------------

    async def begin_turn(self) -> SnapshotLease:
        # Empty body; only Accept header matters for the response format.
        with _tracer.start_as_current_span(spans.SNAPSHOT_LEASE) as span:
            t0 = time.monotonic()
            resp = await self._client.post(
                "/turn/begin", headers={"Accept": CT_PROTOBUF}
            )
            dur_ms = int((time.monotonic() - t0) * 1000)
            span.set_attribute(spans.Attrs.SNAPSHOT_DURATION_MS, dur_ms)
            if resp.status_code >= 400:
                # Annotate the span before raising so a failed lease still
                # carries the failure mode (kind, status) into the trace.
                span.set_attribute("error", True)
                _raise_from_error(resp)
            msg = snap_pb.SnapshotBeginResponse()
            msg.ParseFromString(resp.content)
            span.set_attribute(spans.Attrs.SNAPSHOT_ID, msg.snapshot_id)
            return SnapshotLease(
                snapshot_id=msg.snapshot_id,
                expires_at_ms=msg.expires_at_ms,
                window_secs=msg.window_secs,
            )

    async def end_turn(self, snapshot_id: str) -> None:
        """Idempotent. Failures are logged-and-swallowed because the GC
        sweep cleans up unreleased snapshots within 5 minutes; making
        this fatal would propagate transient network blips into the
        agent's user-facing error path."""
        try:
            req = snap_pb.SnapshotEndRequest(snapshot_id=snapshot_id)
            resp = await self._client.post(
                "/turn/end",
                content=_encode(req),
                headers=HEADERS_PROTO,
            )
            if resp.status_code >= 400:
                log.warning(
                    "turn_end_non_2xx", status=resp.status_code, snapshot_id=snapshot_id
                )
        except Exception as e:  # noqa: BLE001
            log.warning("turn_end_failed", snapshot_id=snapshot_id, error=str(e))

    # -----------------------------------------------------------------
    # Primitives
    # -----------------------------------------------------------------

    async def wallet_profile(
        self,
        *,
        addr: str,
        snapshot_id: str,
        time_scope: scope_pb.TimeScope | None = None,
    ) -> PrimitiveResult:
        """Profile a single wallet against the snapshot. `time_scope`
        defaults to Live (the only v0 supported arm)."""
        ts = time_scope if time_scope is not None else _make_time_scope_live()
        req = env_pb.WalletProfileRequest(
            input=wp_pb.WalletProfileInput(addr=addr, time_scope=ts),
            snapshot_id=snapshot_id,
        )
        with _tracer.start_as_current_span(spans.PRIMITIVE_WALLET_PROFILE) as span:
            span.set_attribute(spans.Attrs.SNAPSHOT_ID, snapshot_id)
            span.set_attribute(spans.Attrs.PRIMITIVE_INPUT_ADDR, addr)
            span.set_attribute(
                spans.Attrs.PRIMITIVE_INPUT,
                _proto_to_capped_json(req, cap=spans.PRIMITIVE_PAYLOAD_MAX_BYTES),
            )
            t0 = time.monotonic()
            resp = await self._client.post(
                "/primitive/wallet_profile",
                content=_encode(req),
                headers=HEADERS_PROTO,
            )
            dur_ms = int((time.monotonic() - t0) * 1000)
            span.set_attribute(spans.Attrs.PRIMITIVE_DURATION_MS, dur_ms)
            if resp.status_code >= 400:
                span.set_attribute("error", True)
                _raise_from_error(resp)
            span.set_attribute(spans.Attrs.PRIMITIVE_OUTPUT_DIGEST, _digest12(resp.content))
            out_env = env_pb.PrimitiveResponseEnvelope()
            out_env.ParseFromString(resp.content)
            span.set_attribute(
                spans.Attrs.PRIMITIVE_OUTPUT,
                _proto_to_capped_json(out_env, cap=spans.PRIMITIVE_PAYLOAD_MAX_BYTES),
            )
            return _decode_envelope(resp.content)

    async def community_summary(
        self,
        *,
        community_id: int,
        snapshot_id: str,
        time_scope: scope_pb.TimeScope | None = None,
    ) -> PrimitiveResult:
        ts = time_scope if time_scope is not None else _make_time_scope_live()
        req = env_pb.CommunitySummaryRequest(
            input=cs_pb.CommunitySummaryInput(
                community_id=community_id,
                time_scope=ts,
            ),
            snapshot_id=snapshot_id,
        )
        with _tracer.start_as_current_span(spans.PRIMITIVE_COMMUNITY_SUMMARY) as span:
            span.set_attribute(spans.Attrs.SNAPSHOT_ID, snapshot_id)
            span.set_attribute(spans.Attrs.PRIMITIVE_INPUT_COMMUNITY_ID, community_id)
            span.set_attribute(
                spans.Attrs.PRIMITIVE_INPUT,
                _proto_to_capped_json(req, cap=spans.PRIMITIVE_PAYLOAD_MAX_BYTES),
            )
            t0 = time.monotonic()
            resp = await self._client.post(
                "/primitive/community_summary",
                content=_encode(req),
                headers=HEADERS_PROTO,
            )
            dur_ms = int((time.monotonic() - t0) * 1000)
            span.set_attribute(spans.Attrs.PRIMITIVE_DURATION_MS, dur_ms)
            if resp.status_code >= 400:
                span.set_attribute("error", True)
                _raise_from_error(resp)
            span.set_attribute(spans.Attrs.PRIMITIVE_OUTPUT_DIGEST, _digest12(resp.content))
            out_env = env_pb.PrimitiveResponseEnvelope()
            out_env.ParseFromString(resp.content)
            span.set_attribute(
                spans.Attrs.PRIMITIVE_OUTPUT,
                _proto_to_capped_json(out_env, cap=spans.PRIMITIVE_PAYLOAD_MAX_BYTES),
            )
            return _decode_envelope(resp.content)


def _encode(msg: Message) -> bytes:
    return msg.SerializeToString()


def _proto_to_capped_json(msg: Message, *, cap: int) -> str:
    """Canonical-JSON-encode a proto message and cap to `cap` bytes.
    On overflow, append a literal truncation marker so probes can
    detect partial payloads without re-parsing JSON."""
    s = json_format.MessageToJson(
        msg,
        preserving_proto_field_name=False,  # canonical camelCase on the wire
        indent=None,
        sort_keys=True,
    )
    if len(s) <= cap:
        return s
    return s[:cap] + f" ...[truncated, total={len(s)}]"
