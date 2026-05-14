"""HTTP client wrapping the Rust data plane internal listener on :8004.

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
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import structlog
from google.protobuf import json_format
from google.protobuf.message import Message
from opentelemetry import trace

from multichain.wire.shared.v1 import (
    community_summary_pb2 as cs_pb,
    get_token_info_pb2 as gti_pb,
    primitive_envelope_pb2 as env_pb,
    scope_pb2 as scope_pb,
    snapshot_pb2 as snap_pb,
    wallet_profile_pb2 as wp_pb,
)

from agent_service import spans

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
class TokenInfo:
    """Decoded `GetTokenInfoOutput`. Returned by `get_token_info`.
    Distinct from `PrimitiveResult` because this primitive returns the
    typed output directly (no `PrimitiveResponseEnvelope` wrapper):
    metadata lookup has no provenance / subgraph_slice surface to
    populate, so the generic envelope would be empty noise.

    `verified` / `canonical_*` are stamped server-side by Rust's
    `canonical_mints::stamp_verification` inside
    `primitives::get_token_info::compute`. The Python tool wrapper
    just passes the fields through; there is no Python-side stamping
    anymore.
    """

    mint: str
    # `name`, `symbol`, `uri`, `update_authority` are absent (None) when
    # the mint exists on chain but has no resolvable metadata. In that
    # case `source_program` is empty too.
    name: str | None
    symbol: str | None
    uri: str | None
    update_authority: str | None
    # "metaplex" | "token2022" | "" (no metadata)
    source_program: str
    # Canonical-mint verification stamp from the Rust registry.
    # `verified=True` iff the mint pubkey is in the canonical registry;
    # `canonical_name` / `canonical_symbol` populated only in that case.
    verified: bool
    canonical_name: str | None
    canonical_symbol: str | None

    @property
    def found(self) -> bool:
        """`True` when the mint has resolvable metadata. Maps to a
        non-empty source_program."""
        return bool(self.source_program)


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
        # Exposed publicly so the codex driver can build the
        # `GET /turn/{snapshot_id}/claims` URL on a separate
        # streaming httpx client (the existing
        # `self._client` is configured for short proto-binary RPCs,
        # not long-lived SSE drains).
        self.base_url = base_url
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_s),
        )

    async def close(self) -> None:
        await self._client.aclose()

    # -----------------------------------------------------------------
    # Snapshot lease
    # -----------------------------------------------------------------

    async def begin_turn(
        self, window_secs: int | None = None
    ) -> SnapshotLease:
        """Lease a snapshot for one agent turn.

        Empty body; only `Accept` header matters for the response
        format. When `window_secs` is set, it's forwarded as
        `?window=N` on the URL so the Rust side materializes that
        window instead of its 60s default. Accepted values are the
        `WINDOWS` enum on the Rust side
        (`[10, 60, 300, 900, 1800, 3600]`); anything else returns
        400 from the data plane and we raise. `None` (default)
        preserves the historical contract for every caller that
        doesn't opt in.
        """
        url = "/turn/begin"
        if window_secs is not None:
            url = f"/turn/begin?window={int(window_secs)}"
        with _tracer.start_as_current_span(spans.SNAPSHOT_LEASE) as span:
            t0 = time.monotonic()
            resp = await self._client.post(
                url, headers={"Accept": CT_PROTOBUF}
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
            # Stamp the resolved window on the lease span so traces
            # can attribute "this turn was over a 15-minute slice"
            # without correlating against the request body. Source of
            # truth is what Rust materialized, not what we asked for.
            span.set_attribute(
                spans.Attrs.SNAPSHOT_WINDOW_SECS, msg.window_secs
            )
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
    # Codex-path claim stream (harness-engineering chunk 2)
    # -----------------------------------------------------------------

    async def stream_claims(
        self, snapshot_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to the per-snapshot SSE drain at
        `GET /turn/{snapshot_id}/claims` and yield each emitted claim
        as a parsed dict. Single-consumer per snapshot enforced by
        the Rust handler (a second subscriber gets HTTP 409).

        The stream closes when the Rust side drops the channel
        sender, which `turn_end` does explicitly. Caller wraps this
        in `async for` and just lets the loop complete naturally.

        Errors during streaming (network blip, snapshot expired
        mid-stream, etc) raise `PrimitiveError`. Caller's contract:
        invoke after the codex sub-loop is started (so emit_claims
        tool calls have somewhere to land) and let the iterator
        terminate when codex exits + turn_end fires.

        Used by the codex harness path only; the Pydantic AI primary
        agent path doesn't go through this  it accumulates claims
        in `deps.emitted_claims` and the loop driver reads that
        buffer directly.
        """
        url = f"/turn/{snapshot_id}/claims"
        try:
            async with self._client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise PrimitiveError(
                        kind="claim_stream_unavailable",
                        message=f"GET {url} -> {resp.status_code}: {body.decode('utf-8', 'replace')[:200]}",
                        status_code=resp.status_code,
                    )
                # Minimal SSE parser: walk lines, accumulate `data:`
                # values until a blank line, then yield the parsed
                # JSON. The Rust handler emits one claim per SSE
                # event with `event: claim`; we filter by event name
                # to ignore future event types (e.g. keep-alive
                # comments, which `aiter_lines` returns as empty
                # strings when they're SSE comment lines).
                event_name: str | None = None
                data_buf: list[str] = []
                async for raw_line in resp.aiter_lines():
                    if raw_line == "":
                        # Frame terminator. Yield if it was a claim
                        # event with actual data; reset state either
                        # way.
                        if event_name == "claim" and data_buf:
                            payload = "\n".join(data_buf)
                            try:
                                yield json.loads(payload)
                            except json.JSONDecodeError as e:
                                log.warning(
                                    "claim_stream_bad_payload",
                                    snapshot_id=snapshot_id,
                                    error=str(e),
                                    payload_head=payload[:120],
                                )
                        event_name = None
                        data_buf = []
                        continue
                    if raw_line.startswith(":"):
                        # SSE comment (KeepAlive). Ignore.
                        continue
                    if raw_line.startswith("event:"):
                        event_name = raw_line[len("event:") :].strip()
                    elif raw_line.startswith("data:"):
                        # Per spec, multi-line data values are joined
                        # with newlines. We accumulate and join at
                        # frame-terminator time.
                        data_buf.append(raw_line[len("data:") :].lstrip())
                    # Other field types (id:, retry:) ignored  the
                    # Rust handler doesn't emit them but a future
                    # version might; silent-skip is safer than
                    # rejecting.
        except httpx.HTTPError as e:
            raise PrimitiveError(
                kind="claim_stream_network_error",
                message=str(e),
                status_code=None,
            ) from e

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

    async def get_token_info(self, *, mint: str) -> TokenInfo:
        """Resolve a mint pubkey to its on-chain `name / symbol / uri`.
        Stateless lookup; doesn't take a snapshot_id (the Rust handler
        ignores the field if passed). Returns `TokenInfo` with
        `source_program == ""` when the mint exists but has no
        resolvable metadata via either Metaplex PDA or Token-2022
        inline extension.

        Returned strings are UNTRUSTED text chosen by the token issuer
        at mint creation. Callers MUST wrap in `<external_data>` (or
        equivalent boundary marker) before surfacing to the model. The
        `agent.py` tool wrapper handles this; direct callers of the
        client must do it themselves.
        """
        req = env_pb.GetTokenInfoRequest(
            input=gti_pb.GetTokenInfoInput(mint=mint),
            snapshot_id="",
        )
        with _tracer.start_as_current_span(spans.PRIMITIVE_GET_TOKEN_INFO) as span:
            span.set_attribute(spans.Attrs.PRIMITIVE_INPUT_MINT, mint)
            span.set_attribute(
                spans.Attrs.PRIMITIVE_INPUT,
                _proto_to_capped_json(req, cap=spans.PRIMITIVE_PAYLOAD_MAX_BYTES),
            )
            t0 = time.monotonic()
            resp = await self._client.post(
                "/primitive/get_token_info",
                content=_encode(req),
                headers=HEADERS_PROTO,
            )
            dur_ms = int((time.monotonic() - t0) * 1000)
            span.set_attribute(spans.Attrs.PRIMITIVE_DURATION_MS, dur_ms)
            if resp.status_code >= 400:
                span.set_attribute("error", True)
                _raise_from_error(resp)
            span.set_attribute(spans.Attrs.PRIMITIVE_OUTPUT_DIGEST, _digest12(resp.content))
            out = gti_pb.GetTokenInfoOutput()
            out.ParseFromString(resp.content)
            span.set_attribute(
                spans.Attrs.PRIMITIVE_OUTPUT,
                _proto_to_capped_json(out, cap=spans.PRIMITIVE_PAYLOAD_MAX_BYTES),
            )
            span.set_attribute(
                spans.Attrs.PRIMITIVE_GET_TOKEN_INFO_SOURCE, out.source_program
            )
            return TokenInfo(
                mint=out.mint,
                # proto3 `optional` fields use `HasField` to distinguish
                # "absent" from "explicitly empty string". Map absent to
                # None so the agent narration loop can branch on it.
                name=out.name if out.HasField("name") else None,
                symbol=out.symbol if out.HasField("symbol") else None,
                uri=out.uri if out.HasField("uri") else None,
                update_authority=(
                    out.update_authority if out.HasField("update_authority") else None
                ),
                source_program=out.source_program,
                verified=out.verified,
                canonical_name=(
                    out.canonical_name if out.HasField("canonical_name") else None
                ),
                canonical_symbol=(
                    out.canonical_symbol if out.HasField("canonical_symbol") else None
                ),
            )


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
