"""Tests for `agent_service.primitive_client.PrimitiveClient`.

Wires httpx mocks at the request level via `pytest-httpx` so the
client's own logic (envelope parsing, error mapping, lease lifecycle)
is under test. No real Rust container.
"""

from __future__ import annotations

import pytest

from agent_service.primitive_client import PrimitiveError
from agent_service.wire.shared import (
    CommunitySummaryInput,
    SnapshotBeginResponse,
    WalletProfileInput,
    WalletProfileOutput,
)

from tests.conftest import DATA_PLANE_BASE
from tests.fixtures import primitive_responses as canned

# ---------------------------------------------------------------------------
# Snapshot lease
# ---------------------------------------------------------------------------


async def test_begin_turn_returns_lease(primitive_client, mock_data_plane):
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/turn/begin",
        json=canned.SNAPSHOT_BEGIN_RESPONSE,
    )
    lease: SnapshotBeginResponse = await primitive_client.begin_turn()
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
        json=canned.WALLET_PROFILE_RESPONSE,
    )
    input_ = WalletProfileInput.model_validate(
        {"addr": canned.WALLET_PROFILE_ADDR, "time_scope": "live"}
    )
    out: WalletProfileOutput = await primitive_client.wallet_profile(
        input_, canned.VALID_SNAPSHOT_ID
    )
    assert out.addr == canned.WALLET_PROFILE_ADDR
    assert out.role.value == "whale"
    assert out.community_id == canned.WALLET_PROFILE_COMMUNITY_ID
    assert out.stats.total_volume_lamports == 80223943444.0
    assert len(out.top_counterparties) == 5


async def test_wallet_profile_not_in_window_raises(primitive_client, mock_data_plane):
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/primitive/wallet_profile",
        status_code=404,
        json=canned.WALLET_NOT_IN_WINDOW_ERROR,
    )
    input_ = WalletProfileInput.model_validate(
        {"addr": canned.WALLET_PROFILE_ADDR, "time_scope": "live"}
    )
    with pytest.raises(PrimitiveError) as excinfo:
        await primitive_client.wallet_profile(input_, canned.VALID_SNAPSHOT_ID)
    assert excinfo.value.kind == "not_in_window"
    assert excinfo.value.status_code == 404


async def test_wallet_profile_snapshot_gone_raises(primitive_client, mock_data_plane):
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/primitive/wallet_profile",
        status_code=410,
        json=canned.SNAPSHOT_GONE_ERROR,
    )
    input_ = WalletProfileInput.model_validate(
        {"addr": "X", "time_scope": "live"}
    )
    with pytest.raises(PrimitiveError) as excinfo:
        await primitive_client.wallet_profile(input_, "stale-snapshot-id")
    assert excinfo.value.kind == "snapshot_gone"
    assert excinfo.value.status_code == 410


async def test_wallet_profile_internal_error(primitive_client, mock_data_plane):
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/primitive/wallet_profile",
        status_code=500,
        json={"error": "boom", "kind": "internal"},
    )
    input_ = WalletProfileInput.model_validate(
        {"addr": "X", "time_scope": "live"}
    )
    with pytest.raises(PrimitiveError) as excinfo:
        await primitive_client.wallet_profile(input_, canned.VALID_SNAPSHOT_ID)
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
    input_ = WalletProfileInput.model_validate(
        {"addr": "X", "time_scope": "live"}
    )
    with pytest.raises(PrimitiveError) as excinfo:
        await primitive_client.wallet_profile(input_, canned.VALID_SNAPSHOT_ID)
    assert excinfo.value.status_code == 502
    assert "Bad Gateway" in excinfo.value.message


# ---------------------------------------------------------------------------
# community_summary
# ---------------------------------------------------------------------------


async def test_community_summary_happy_path(primitive_client, mock_data_plane):
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/primitive/community_summary",
        json=canned.COMMUNITY_SUMMARY_RESPONSE,
    )
    input_ = CommunitySummaryInput.model_validate(
        {"community_id": canned.WALLET_PROFILE_COMMUNITY_ID, "time_scope": "live"}
    )
    out = await primitive_client.community_summary(input_, canned.VALID_SNAPSHOT_ID)
    assert out.community_id == canned.WALLET_PROFILE_COMMUNITY_ID
    assert out.size == 7
    assert out.edge_count == 6
    assert len(out.top_wallets) == 2


# ---------------------------------------------------------------------------
# Snapshot_id is actually sent (not silently dropped)
# ---------------------------------------------------------------------------


async def test_wallet_profile_sends_snapshot_id_in_body(
    primitive_client, mock_data_plane
):
    """Regression guard for the snapshot lease: the client MUST
    serialize `snapshot_id` into the request body, not drop it. If
    a refactor breaks this, every primitive call returns 410 Gone
    against the real Rust route."""
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/primitive/wallet_profile",
        json=canned.WALLET_PROFILE_RESPONSE,
    )
    input_ = WalletProfileInput.model_validate(
        {"addr": canned.WALLET_PROFILE_ADDR, "time_scope": "live"}
    )
    await primitive_client.wallet_profile(input_, canned.VALID_SNAPSHOT_ID)

    requests = mock_data_plane.get_requests()
    assert len(requests) == 1
    body = requests[0].read().decode()
    assert canned.VALID_SNAPSHOT_ID in body
    assert canned.WALLET_PROFILE_ADDR in body
