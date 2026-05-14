"""FastAPI router serving the Rust data-plane HTTP surface that
pydantic-ai's `PrimitiveClient` calls. Binary-protobuf in/out where
Rust does so today; the request envelopes carry `snapshot_id` plus
the primitive input, and responses are either
`PrimitiveResponseEnvelope` (wallet_profile, community_summary) or
the typed proto output directly (get_token_info).

Encoders reused from `agent_service.test_support.primitive_responses`
so the mock and the integration tests share one source of truth for
the canned wire bytes.

Snapshot lifecycle: `/turn/begin` mints a ULID-shaped id and
registers it on the shared `FixtureStore`; `/turn/end` removes it
and signals end-of-stream on the per-snapshot claim queue. The SSE
drain at `/turn/{snapshot_id}/claims` consumes from that queue;
single-consumer per snapshot is the contract (the hermetic runner
opens at most one drain per turn).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from agent_service.test_support.primitive_responses import (
    SNAPSHOT_BEGIN_RESPONSE,
    encode_community_summary_response,
    encode_get_token_info_response,
    encode_snapshot_begin_response,
    encode_wallet_profile_response,
)
from multichain.wire.shared.v1 import (
    community_summary_pb2 as cs_pb,  # noqa: F401  imported for proto registry side-effects
    get_token_info_pb2 as gti_pb,
    primitive_envelope_pb2 as env_pb,
    snapshot_pb2 as snap_pb,
)

from eval_mock.fixtures import STORE

log = structlog.get_logger(__name__)

CT_PROTOBUF = "application/x-protobuf"

router = APIRouter()


def _mint_snapshot_id() -> str:
    # The Rust side uses ULIDs (`01HXYZ...`); for the mock a short
    # random hex is fine. Pydantic-ai's PrimitiveClient + agent-side
    # codex driver both treat snapshot_id as opaque.
    return f"01HMOCK{uuid.uuid4().hex[:18].upper()}"


# ---------------------------------------------------------------------------
# Snapshot lease
# ---------------------------------------------------------------------------


@router.post("/turn/begin")
async def turn_begin(request: Request) -> Response:
    """Mint a fresh snapshot id and register it on the fixture store.
    Window query param is honored shape-wise (returned in the
    response) but the mock doesn't model multiple windows: the
    fixture store is window-agnostic and the deterministic outputs
    don't depend on which window the case asked for."""
    window_secs = int(request.query_params.get("window", "60"))
    snapshot_id = _mint_snapshot_id()
    STORE.register_snapshot(snapshot_id)
    msg = snap_pb.SnapshotBeginResponse(
        snapshot_id=snapshot_id,
        expires_at_ms=int(time.time() * 1000) + 5 * 60 * 1000,
        window_secs=window_secs,
    )
    return Response(content=msg.SerializeToString(), media_type=CT_PROTOBUF)


@router.post("/turn/end")
async def turn_end(request: Request) -> Response:
    body = await request.body()
    req = snap_pb.SnapshotEndRequest()
    try:
        req.ParseFromString(body)
    except Exception:  # noqa: BLE001
        # Match Rust's lenient parse on this idempotent end call;
        # log but don't 400.
        log.warning("turn_end_parse_failed", body_len=len(body))
    STORE.end_snapshot(req.snapshot_id)
    return Response(status_code=204)


@router.get("/turn/{snapshot_id}/claims")
async def stream_claims(snapshot_id: str) -> StreamingResponse:
    """SSE drain matching Rust's per-snapshot mpsc channel. The
    FastMCP `emit_claims` handler in `mcp_proxy` pushes claim dicts
    onto the snapshot's queue; this handler consumes and emits one
    SSE `event: claim` per claim. Sentinel `None` from the queue
    means `/turn/end` fired, exit the loop."""

    queue = STORE.claim_queues.get(snapshot_id)
    if queue is None:
        # The pydantic-ai path never opens this drain (it pushes to
        # `deps.emitted_claims` instead), so the only legitimate
        # caller is the codex loop driver after `/turn/begin`.
        # Surface a 404 so the driver can fail fast.
        raise HTTPException(
            status_code=404,
            detail=f"no claim queue for snapshot_id={snapshot_id!r}; "
            "the snapshot was never opened or has been ended",
        )

    async def event_gen():
        while True:
            item = await queue.get()
            if item is None:
                return
            # Match Rust's frame shape: `event: claim\ndata: <json>\n\n`.
            import json

            yield (
                "event: claim\n"
                f"data: {json.dumps(item, separators=(',', ':'))}\n\n"
            )

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def _wallet_profile_payload(addr: str) -> dict[str, Any]:
    """Resolve the fixture-store entry for `addr`, falling back to an
    empty-shape default so cases that don't pin a wallet_profile
    fixture still get a parseable response (matches the live behavior
    for an unknown wallet: the route returns valid bytes; downstream
    semantic checks live in probes)."""
    fixture = STORE.wallet_profile.get(addr)
    if fixture is not None:
        return fixture.payload
    # Default empty shape. Matches the proto types' zero/empty values
    # so the envelope encoder still produces parseable bytes.
    return {
        "addr": addr,
        "role": "NODE_ROLE_UNKNOWN",
        "community_id": 0,
        "stats": {
            "degree": 0,
            "total_volume_lamports": 0.0,
            "in_volume_lamports": 0.0,
            "out_volume_lamports": 0.0,
            "bidir_volume_lamports": 0.0,
            "sol_degree": 0,
            "spl_degree": 0,
        },
        "top_counterparties": [],
        "age_in_window_secs": 0,
    }


def _community_summary_payload(community_id: int) -> dict[str, Any]:
    fixture = STORE.community_summary.get(community_id)
    if fixture is not None:
        return fixture.payload
    return {
        "community_id": community_id,
        "size": 0,
        "total_volume": 0.0,
        "internal_volume": 0.0,
        "external_volume": 0.0,
        "edge_count": 0,
        "top_wallets": [],
    }


@router.post("/primitive/wallet_profile")
async def wallet_profile_route(request: Request) -> Response:
    body = await request.body()
    req = env_pb.WalletProfileRequest()
    req.ParseFromString(body)
    payload = _wallet_profile_payload(req.input.addr)
    # The encoder needs `value` + `provenance`. For the default empty
    # shape we emit empty provenance; fixture-defined wallets carry
    # their own provenance refs.
    fixture_dict = {
        "value": payload,
        "provenance": [{"kind": "wallet", "addr": req.input.addr, "idx": 0}],
    }
    return Response(
        content=_encode_envelope_dict(fixture_dict),
        media_type=CT_PROTOBUF,
    )


@router.post("/primitive/community_summary")
async def community_summary_route(request: Request) -> Response:
    body = await request.body()
    req = env_pb.CommunitySummaryRequest()
    req.ParseFromString(body)
    payload = _community_summary_payload(req.input.community_id)
    fixture_dict = {
        "value": payload,
        "provenance": [{"kind": "community", "id": req.input.community_id}],
    }
    return Response(
        content=_encode_envelope_dict(fixture_dict),
        media_type=CT_PROTOBUF,
    )


@router.post("/primitive/get_token_info")
async def get_token_info_route(request: Request) -> Response:
    body = await request.body()
    req = env_pb.GetTokenInfoRequest()
    req.ParseFromString(body)
    mint = req.input.mint.strip()
    if not mint:
        raise HTTPException(status_code=400, detail="mint must be non-empty")

    fixture = STORE.get_token_info.get(mint)
    if fixture is None:
        # Unknown mint, empty-metadata shape. Live Rust would 503 if
        # RPC is disabled; in hermetic mode we always have a defined
        # response: an empty stamp counts as "exists, no metadata."
        payload = {
            "mint": mint,
            "name": None,
            "symbol": None,
            "uri": None,
            "update_authority": None,
            "source_program": "",
        }
    else:
        payload = {
            "mint": fixture.mint,
            "name": fixture.name,
            "symbol": fixture.symbol,
            "uri": fixture.uri,
            "update_authority": fixture.update_authority,
            "source_program": fixture.source_program,
        }

    # Mirror Rust's `canonical_mints::stamp_verification`. The
    # canonical-mint registry lives in `backend/src/canonical_mints.rs`;
    # this small local copy stamps the same three fields the same way
    # so the impostor case lands `verified=false` and canonical pubkeys
    # land `verified=true`.
    _stamp_verification(payload)

    encoded = encode_get_token_info_response(payload)
    return Response(content=encoded, media_type=CT_PROTOBUF)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Mirror of `backend/src/canonical_mints.rs::REGISTRY`. Kept tiny on
# purpose: the production set today is USDC / USDT / wSOL plus the
# SOL sentinel; expanding the registry is a Rust-side change that the
# Rust drift test will surface via the schema snapshot (canonical-mint
# behavior is observable as the `verified` flag on `tools/list`'s tool
# responses, not on the tools/list output itself  so adding a mint
# here requires manually tracking the Rust change. A follow-on can
# dump the registry to JSON like the schema snapshot, removing the
# duplication entirely).
_CANONICAL_REGISTRY: dict[str, tuple[str, str]] = {
    # mint pubkey -> (canonical_name, canonical_symbol)
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": ("USD Coin", "USDC"),
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": ("USD Tether", "USDT"),
    "So11111111111111111111111111111111111111112": ("Wrapped SOL", "wSOL"),
}


def _stamp_verification(payload: dict[str, Any]) -> None:
    """Replicate Rust's canonical-mint stamp. Keeps the mock's
    impostor-case behavior aligned with live Rust without each
    response carrying hand-typed verified flags."""
    mint = payload.get("mint", "")
    entry = _CANONICAL_REGISTRY.get(mint)
    if entry is None:
        payload["verified"] = False
        payload["canonical_name"] = None
        payload["canonical_symbol"] = None
    else:
        canonical_name, canonical_symbol = entry
        payload["verified"] = True
        payload["canonical_name"] = canonical_name
        payload["canonical_symbol"] = canonical_symbol


def _encode_envelope_dict(envelope: dict[str, Any]) -> bytes:
    """Wraps the agent-service `_envelope_bytes` helper. Keeps the
    binary-encoding path here so the mock has one place to evolve when
    the envelope shape changes."""
    # Import lazily so the module-load path doesn't pull encoder
    # machinery for routes that don't need it.
    from agent_service.test_support.primitive_responses import _envelope_bytes

    return _envelope_bytes(envelope["value"], envelope["provenance"])


# Re-export the bare-snapshot encoder for any caller that wants to
# emit a default lease without going through `/turn/begin` (none
# today; reserved for tests that construct the response inline).
__all__ = [
    "router",
    "encode_snapshot_begin_response",
    "encode_wallet_profile_response",
    "encode_community_summary_response",
    "encode_get_token_info_response",
    "SNAPSHOT_BEGIN_RESPONSE",
]
