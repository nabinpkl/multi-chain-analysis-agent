"""Tests for `agent_service.primitive_client.PrimitiveClient`.

Stage 2 of the proto migration: production wire is binary protobuf.
Mocks return proto-encoded bytes via the `encode_*` helpers in
`fixtures/primitive_responses.py`. Errors stay JSON-shaped on the
Rust side regardless of request format, so error mocks still use
`json={...}`.

No real Rust container.
"""

from __future__ import annotations

import pytest

from agent_service.primitive_client import (
    PrimitiveError,
    PrimitiveResult,
    SnapshotLease,
)

from tests.conftest import DATA_PLANE_BASE
from tests.fixtures import primitive_responses as canned

PROTO_CT = {"Content-Type": "application/x-protobuf"}


# ---------------------------------------------------------------------------
# Snapshot lease
# ---------------------------------------------------------------------------


async def test_begin_turn_returns_lease(primitive_client, mock_data_plane):
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/turn/begin",
        content=canned.encode_snapshot_begin_response(),
        headers=PROTO_CT,
    )
    lease: SnapshotLease = await primitive_client.begin_turn()
    assert lease.snapshot_id == canned.VALID_SNAPSHOT_ID
    assert lease.window_secs == 60
    assert lease.expires_at_ms > 0


async def test_end_turn_204_no_error(primitive_client, mock_data_plane):
    mock_data_plane.add_response(
        method="POST", url=f"{DATA_PLANE_BASE}/turn/end", status_code=204
    )
    # Should return None and not raise.
    result = await primitive_client.end_turn(canned.VALID_SNAPSHOT_ID)
    assert result is None


async def test_end_turn_swallows_5xx(primitive_client, mock_data_plane):
    """`end_turn` is fire-and-forget; ClickHouse / Rust hiccups
    must not propagate into the agent's user-facing error path."""
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/turn/end",
        status_code=500,
        json={"error": "internal", "kind": "internal"},
    )
    # No exception expected.
    await primitive_client.end_turn(canned.VALID_SNAPSHOT_ID)


async def test_end_turn_swallows_network_error(primitive_client, mock_data_plane):
    import httpx

    mock_data_plane.add_exception(
        httpx.ConnectError("simulated"), method="POST", url=f"{DATA_PLANE_BASE}/turn/end"
    )
    await primitive_client.end_turn(canned.VALID_SNAPSHOT_ID)


# ---------------------------------------------------------------------------
# wallet_profile
# ---------------------------------------------------------------------------


async def test_wallet_profile_happy_path(primitive_client, mock_data_plane):
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/primitive/wallet_profile",
        content=canned.encode_wallet_profile_response(),
        headers=PROTO_CT,
    )
    result: PrimitiveResult = await primitive_client.wallet_profile(
        addr=canned.WALLET_PROFILE_ADDR,
        snapshot_id=canned.VALID_SNAPSHOT_ID,
    )
    assert result.value["addr"] == canned.WALLET_PROFILE_ADDR
    assert result.value["role"] == "NODE_ROLE_WHALE"
    assert result.value["community_id"] == canned.WALLET_PROFILE_COMMUNITY_ID
    assert result.value["stats"]["total_volume_lamports"] == 80223943444.0
    assert len(result.value["top_counterparties"]) == 5
    # Provenance stays typed: list of proto ProvenanceRef.
    assert len(result.provenance) == len(canned.WALLET_PROFILE_RESPONSE["provenance"])


async def test_wallet_profile_not_in_window_raises(primitive_client, mock_data_plane):
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/primitive/wallet_profile",
        status_code=404,
        json=canned.WALLET_NOT_IN_WINDOW_ERROR,
    )
    with pytest.raises(PrimitiveError) as excinfo:
        await primitive_client.wallet_profile(
            addr=canned.WALLET_PROFILE_ADDR,
            snapshot_id=canned.VALID_SNAPSHOT_ID,
        )
    assert excinfo.value.kind == "not_in_window"
    assert excinfo.value.status_code == 404


async def test_wallet_profile_snapshot_gone_raises(primitive_client, mock_data_plane):
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/primitive/wallet_profile",
        status_code=410,
        json=canned.SNAPSHOT_GONE_ERROR,
    )
    with pytest.raises(PrimitiveError) as excinfo:
        await primitive_client.wallet_profile(
            addr="X",
            snapshot_id="stale-snapshot-id",
        )
    assert excinfo.value.kind == "snapshot_gone"
    assert excinfo.value.status_code == 410


async def test_wallet_profile_internal_error(primitive_client, mock_data_plane):
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/primitive/wallet_profile",
        status_code=500,
        json={"error": "boom", "kind": "internal"},
    )
    with pytest.raises(PrimitiveError) as excinfo:
        await primitive_client.wallet_profile(
            addr="X", snapshot_id=canned.VALID_SNAPSHOT_ID
        )
    assert excinfo.value.kind == "internal"


async def test_wallet_profile_non_json_error_body(primitive_client, mock_data_plane):
    """Defensive: error path falls back to text body when payload
    isn't JSON. Real Rust always returns JSON, but proxies / load
    balancers can inject HTML 502 pages."""
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/primitive/wallet_profile",
        status_code=502,
        text="<html>Bad Gateway</html>",
    )
    with pytest.raises(PrimitiveError) as excinfo:
        await primitive_client.wallet_profile(
            addr="X", snapshot_id=canned.VALID_SNAPSHOT_ID
        )
    assert excinfo.value.status_code == 502
    assert "Bad Gateway" in excinfo.value.message


# ---------------------------------------------------------------------------
# community_summary
# ---------------------------------------------------------------------------


async def test_community_summary_happy_path(primitive_client, mock_data_plane):
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/primitive/community_summary",
        content=canned.encode_community_summary_response(),
        headers=PROTO_CT,
    )
    result = await primitive_client.community_summary(
        community_id=canned.WALLET_PROFILE_COMMUNITY_ID,
        snapshot_id=canned.VALID_SNAPSHOT_ID,
    )
    assert result.value["community_id"] == canned.WALLET_PROFILE_COMMUNITY_ID
    assert result.value["size"] == 7
    assert result.value["edge_count"] == 6
    assert len(result.value["top_wallets"]) == 2


# ---------------------------------------------------------------------------
# Wire-format guarantees: snapshot_id rides in the body, content-type
# is binary protobuf, request is decodable as the expected proto type.
# ---------------------------------------------------------------------------


async def test_wallet_profile_sends_binary_proto_with_snapshot_id(
    primitive_client, mock_data_plane
):
    """Regression guard: the client serializes the request as binary
    protobuf with the snapshot_id field set. If a refactor breaks
    this, every primitive call returns 410 Gone against real Rust."""
    from multichain.wire.shared.v1 import primitive_envelope_pb2 as env_pb

    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/primitive/wallet_profile",
        content=canned.encode_wallet_profile_response(),
        headers=PROTO_CT,
    )
    await primitive_client.wallet_profile(
        addr=canned.WALLET_PROFILE_ADDR,
        snapshot_id=canned.VALID_SNAPSHOT_ID,
    )

    requests = mock_data_plane.get_requests()
    assert len(requests) == 1
    sent = requests[0]
    assert sent.headers.get("content-type") == "application/x-protobuf"
    assert sent.headers.get("accept") == "application/x-protobuf"

    # Round-trip the bytes back through the proto type to verify shape.
    decoded = env_pb.WalletProfileRequest()
    decoded.ParseFromString(sent.read())
    assert decoded.snapshot_id == canned.VALID_SNAPSHOT_ID
    assert decoded.input.addr == canned.WALLET_PROFILE_ADDR
    assert decoded.input.time_scope.HasField("live")
