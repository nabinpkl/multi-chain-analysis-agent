"""Wire-type round-trip tests.

For every generated pydantic model in `wire/shared/`, parse a canned
JSON example and assert it round-trips byte-equivalently. If
`datamodel-codegen` ever changes its file generation behavior or the
Rust source changes shape, these tests break loudly.

Includes the Phase A regression test (the per-file class identity
bug we hit) so it can never silently come back.
"""

from __future__ import annotations

import json

import pytest

from agent_service.wire.shared import (
    CommunitySummaryInput,
    CommunitySummaryOutput,
    CommunitySummaryRequest,
    NodeRole,
    NodeStatsWire,
    SnapshotBeginResponse,
    SnapshotEndRequest,
    TopCounterparty,
    TopWallet,
    WalletProfileInput,
    WalletProfileOutput,
    WalletProfileRequest,
)

from tests.fixtures import primitive_responses as canned


# ---------------------------------------------------------------------------
# Round-trip helper
# ---------------------------------------------------------------------------


def _round_trip(model_cls, payload: dict | str) -> dict:
    """Parse → dump → parse → dump. Returns the final dict, which
    must equal the first dict if the schema and serializer agree."""
    if isinstance(payload, str):
        payload = json.loads(payload)
    once = model_cls.model_validate(payload)
    twice = model_cls.model_validate_json(once.model_dump_json())
    return twice.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Per-type round-trip tests
# ---------------------------------------------------------------------------


def test_wallet_profile_input_round_trip():
    payload = {"addr": "X", "time_scope": "live"}
    out = _round_trip(WalletProfileInput, payload)
    assert out["addr"] == "X"
    assert out["time_scope"] == "live"


def test_wallet_profile_output_round_trip():
    out = _round_trip(WalletProfileOutput, canned.WALLET_PROFILE_RESPONSE["value"])
    assert out["addr"] == canned.WALLET_PROFILE_ADDR
    assert out["role"] == "whale"
    assert out["community_id"] == canned.WALLET_PROFILE_COMMUNITY_ID
    assert out["stats"]["total_volume_lamports"] == 80223943444.0
    assert len(out["top_counterparties"]) == 5


def test_community_summary_input_round_trip():
    payload = {"community_id": 8, "time_scope": "live"}
    out = _round_trip(CommunitySummaryInput, payload)
    assert out["community_id"] == 8


def test_community_summary_output_round_trip():
    out = _round_trip(
        CommunitySummaryOutput, canned.COMMUNITY_SUMMARY_RESPONSE["value"]
    )
    assert out["community_id"] == canned.WALLET_PROFILE_COMMUNITY_ID
    assert out["size"] == 7
    assert out["edge_count"] == 6
    assert len(out["top_wallets"]) == 2


def test_snapshot_begin_response_round_trip():
    out = _round_trip(SnapshotBeginResponse, canned.SNAPSHOT_BEGIN_RESPONSE)
    assert out["snapshot_id"] == canned.VALID_SNAPSHOT_ID
    assert out["window_secs"] == 60


def test_snapshot_end_request_round_trip():
    payload = {"snapshot_id": canned.VALID_SNAPSHOT_ID}
    out = _round_trip(SnapshotEndRequest, payload)
    assert out == payload


def test_node_stats_wire_round_trip():
    out = _round_trip(NodeStatsWire, canned.WALLET_PROFILE_RESPONSE["value"]["stats"])
    assert out["degree"] == 5


def test_top_counterparty_round_trip():
    out = _round_trip(
        TopCounterparty, canned.WALLET_PROFILE_RESPONSE["value"]["top_counterparties"][0]
    )
    assert out["volume"] == 50000000000.0


def test_top_wallet_round_trip():
    out = _round_trip(
        TopWallet, canned.COMMUNITY_SUMMARY_RESPONSE["value"]["top_wallets"][0]
    )
    assert out["addr"] == canned.WALLET_PROFILE_ADDR


# ---------------------------------------------------------------------------
# Enum round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wire_value",
    [
        "token-mint",
        "tip-account",
        "mev-searcher",
        "multi-hub",
        "sol-hub",
        "spl-hub",
        "whale",
        "mpc-member",
        "normal",
    ],
)
def test_node_role_kebab_case_round_trip(wire_value: str):
    """Rust serializes NodeRole as kebab-case; pydantic StrEnum must
    accept that exact string and re-emit it identically."""
    role = NodeRole(wire_value)
    assert role.value == wire_value
    # And via `model_validate` on a containing model:
    out = WalletProfileOutput.model_validate(
        {
            **canned.WALLET_PROFILE_RESPONSE["value"],
            "role": wire_value,
        }
    )
    assert out.role == NodeRole(wire_value)


# ---------------------------------------------------------------------------
# Phase A regression: per-file class identity
# ---------------------------------------------------------------------------


def test_wallet_profile_request_accepts_dict_input():
    """`WalletProfileRequest` defines its own internal copy of
    `WalletProfileInput` (datamodel-codegen quirk). Constructing it
    by passing the canonical `WalletProfileInput` instance triggers
    a pydantic class-identity validation error.

    `primitive_client.py` works around this by passing dicts. If
    that workaround ever gets reverted (or if datamodel-codegen
    fixes the per-file generation, which would be a breaking change
    for our wrapper), this test surfaces it.
    """
    body = {
        "input": {"addr": "X", "time_scope": "live"},
        "snapshot_id": canned.VALID_SNAPSHOT_ID,
    }
    req = WalletProfileRequest.model_validate(body)
    assert req.snapshot_id == canned.VALID_SNAPSHOT_ID


def test_wallet_profile_request_rejects_typed_input_directly():
    """Documents the bug: building WalletProfileRequest with a
    canonical WalletProfileInput instance fails. If this test starts
    failing (i.e., datamodel-codegen fixed it), we can safely drop
    the dict round-trip in primitive_client.py.
    """
    canonical = WalletProfileInput.model_validate(
        {"addr": "X", "time_scope": "live"}
    )
    with pytest.raises(Exception):  # noqa: BLE001 - pydantic ValidationError
        WalletProfileRequest(input=canonical, snapshot_id=canned.VALID_SNAPSHOT_ID)


def test_community_summary_request_accepts_dict_input():
    body = {
        "input": {"community_id": 8, "time_scope": "live"},
        "snapshot_id": canned.VALID_SNAPSHOT_ID,
    }
    req = CommunitySummaryRequest.model_validate(body)
    assert req.snapshot_id == canned.VALID_SNAPSHOT_ID
