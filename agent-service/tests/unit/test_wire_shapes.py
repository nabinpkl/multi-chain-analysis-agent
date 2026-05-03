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


# ---------------------------------------------------------------------------
# Phase I.1 agent-only wire types
# ---------------------------------------------------------------------------


def test_agent_request_round_trip():
    """Full AgentRequest with ViewContext + switches + show_trace."""
    from agent_service.wire.agent import AgentRequest

    payload = {
        "user_question": "Profile this wallet",
        "context": {
            "live_window_secs": 60,
            "focus": {"kind": "wallet", "id": "X"},
            "selection": [],
        },
        "show_trace": False,
    }
    req = AgentRequest.model_validate(payload)
    assert req.context.focus.kind == "wallet"
    # Switches default to production preset.
    assert req.switches.stay_in_role is True
    assert req.switches.dont_fabricate is True
    assert req.switches.dont_repeat_yourself is True
    assert req.switches.cross_check.paraphrase_aware_match is True
    assert req.switches.cross_check.ground_truth_match is False


def test_agent_request_rejects_extra_fields():
    """`extra='forbid'`: a typo like `focus_addr` (the old Phase 0
    field) is a 422 instead of silent drop."""
    from agent_service.wire.agent import AgentRequest
    import pydantic

    bad = {
        "user_question": "q",
        "context": {"live_window_secs": 60, "focus": None, "selection": []},
        "focus_addr": "X",
    }
    with pytest.raises(pydantic.ValidationError):
        AgentRequest.model_validate(bad)


def test_claim_full_shape_round_trip():
    """Full Claim shape matching Rust agent::types::Claim."""
    from agent_service.wire.agent import Claim

    payload = {
        "id": "01HKQ0000000000000000FIX01",
        "session_id": "0000000000000000000000000000ffff",
        "kind": "profile",
        "headline": "Wallet ${ref:0} dominates",
        "body_markdown": "Volume ${ref:1} lamports.",
        "provenance": [
            {"kind": "wallet", "addr": "X", "idx": 0},
            {
                "kind": "number",
                "metric": "total_volume_lamports",
                "value": 1.0,
                "support": ["X"],
            },
        ],
        "support_numbers": [{"metric": "vol", "value": 1.0}],
        "subgraph_slice": None,
        "policy_verdict": {"verdict": "approved"},
        "stubs_active": [],
        "emitted_at_ms": 100,
    }
    claim = Claim.model_validate(payload)
    assert claim.kind == "profile"
    assert claim.policy_verdict.verdict == "approved"
    assert claim.emitted_at_ms == 100
    # Round-trip preserves discriminator tag.
    again = Claim.model_validate_json(claim.model_dump_json())
    assert again.policy_verdict.verdict == "approved"


def test_policy_verdict_retracted_round_trip():
    """`{"verdict": "retracted", "reason": "..."}` resolves to the
    PolicyVerdictRetracted variant via discriminator."""
    from agent_service.wire.agent import Claim

    base = {
        "id": "x",
        "session_id": "y",
        "kind": "profile",
        "headline": "h",
        "body_markdown": "b",
        "provenance": [],
        "support_numbers": [],
        "subgraph_slice": None,
        "policy_verdict": {"verdict": "retracted", "reason": "out of bounds"},
        "stubs_active": [],
        "emitted_at_ms": 0,
    }
    claim = Claim.model_validate(base)
    assert claim.policy_verdict.verdict == "retracted"
    assert claim.policy_verdict.reason == "out of bounds"


def test_path_state_three_variants_round_trip():
    """All three PathState variants resolve via the `state` discriminator."""
    from agent_service.wire.agent import PathStep

    for payload in [
        {"stage": "x", "state": {"state": "approved"}, "elapsed_us": 1, "note": "n"},
        {
            "stage": "x",
            "state": {"state": "retracted", "reason": "r"},
            "elapsed_us": 1,
            "note": "n",
        },
        {
            "stage": "x",
            "state": {"state": "not_applicable", "detail": "d"},
            "elapsed_us": 1,
            "note": "n",
        },
    ]:
        step = PathStep.model_validate(payload)
        assert step.state.state == payload["state"]["state"]


def test_field_change_three_kinds_round_trip():
    """FieldChange discriminator dispatches on `kind`."""
    from agent_service.wire.agent import FieldDelta

    for payload in [
        {
            "field_path": "x",
            "primitive": "wallet_profile",
            "change": {"kind": "number_moved", "prior": 1.0, "current": 2.0, "pct": 1.0},
        },
        {
            "field_path": "y",
            "primitive": "wallet_profile",
            "change": {"kind": "set_changed", "added": ["A"], "removed": []},
        },
        {
            "field_path": "z",
            "primitive": "wallet_profile",
            "change": {"kind": "count_changed", "prior": 1.0, "current": 3.0},
        },
    ]:
        fd = FieldDelta.model_validate(payload)
        assert fd.change.kind == payload["change"]["kind"]


def test_constitution_verdict_lenient_parse():
    """ConstitutionVerdict ignores extra keys (matches Rust serde
    `default` behavior). LLM occasionally adds fields; we don't
    crash on them."""
    from agent_service.wire.agent import ConstitutionVerdict

    # Extra `confidence` key is ignored.
    payload = {
        "verdict": "approve",
        "reason": "looks fine",
        "extraction": {"narrative_numbers": [], "claim_numbers": []},
        "confidence": 0.9,
    }
    cv = ConstitutionVerdict.model_validate(payload)
    assert cv.verdict == "approve"
    assert cv.extraction is not None


def test_constitution_verdict_minimal_parse():
    """Only `verdict` is required; reason defaults to "" and
    extraction to None. Matches Rust GateResponse."""
    from agent_service.wire.agent import ConstitutionVerdict

    cv = ConstitutionVerdict.model_validate({"verdict": "retract"})
    assert cv.verdict == "retract"
    assert cv.reason == ""
    assert cv.extraction is None


def test_entity_ref_three_kinds():
    """EntityRef discriminator. Wallet/Edge use string id; Community
    uses int id."""
    from agent_service.wire.agent import (
        EntityRefCommunity,
        EntityRefEdge,
        EntityRefWallet,
        ViewContext,
    )

    ctx = ViewContext(
        live_window_secs=60,
        focus=EntityRefWallet(id="W"),
        selection=[EntityRefEdge(id="0:1"), EntityRefCommunity(id=8)],
    )
    dump = ctx.model_dump(mode="json")
    assert dump["focus"] == {"kind": "wallet", "id": "W"}
    assert dump["selection"][0] == {"kind": "edge", "id": "0:1"}
    assert dump["selection"][1] == {"kind": "community", "id": 8}


def test_changed_since_full_round_trip():
    """Full ship-4 ChangedSince payload."""
    from agent_service.wire.agent import ChangedSince

    payload = {
        "prior_turn": 1,
        "delta": {
            "changed": [
                {
                    "field_path": "stats.in_volume_lamports",
                    "primitive": "wallet_profile",
                    "change": {
                        "kind": "number_moved",
                        "prior": 1.0,
                        "current": 2.0,
                        "pct": 1.0,
                    },
                }
            ],
            "unchanged_field_count": 4,
        },
        "prose": "Volume rose.",
    }
    cs = ChangedSince.model_validate(payload)
    assert cs.prior_turn == 1
    assert cs.delta.changed[0].change.kind == "number_moved"


def test_no_movement_round_trip():
    from agent_service.wire.agent import NoMovement

    nm = NoMovement.model_validate(
        {"prior_turn": 2, "primitives_replayed": ["wallet_profile"]}
    )
    assert nm.prior_turn == 2


def test_narrative_with_refs_round_trip():
    from agent_service.wire.agent import NarrativeWithRefs

    payload = {
        "text": "Wallet ${ref:0} is heavy.",
        "provenance": [{"kind": "wallet", "addr": "X", "idx": 0}],
    }
    n = NarrativeWithRefs.model_validate(payload)
    assert "${ref:0}" in n.text


def test_agent_done_shape():
    """Closer event payload: session_id + elapsed_ms (u32 in Rust)."""
    from agent_service.wire.agent import AgentDone

    d = AgentDone.model_validate({"session_id": "abc", "elapsed_ms": 1234})
    assert d.elapsed_ms == 1234
