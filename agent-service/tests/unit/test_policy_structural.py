"""Parity tests for the structural value compare. Mirror of
`policy_structural.rs`'s test set. Builds proto ProvenanceRef messages
directly so the gate exercises the same wire shape the loop driver
hands it."""

from __future__ import annotations

from agent_service.policy.binding_store import PrimitiveBindingStore, build_binding
from agent_service.policy.crosscheck import UnitClass
from agent_service.policy.structural import MismatchError, verify_chip_values
from multichain.wire.shared.v1 import provenance_pb2 as prov_pb


def _wallet(addr: str, idx: int | None = None) -> prov_pb.ProvenanceRef:
    w = prov_pb.WalletRef(addr=addr)
    if idx is not None:
        w.idx = idx
    return prov_pb.ProvenanceRef(wallet=w)


def _community(cid: int) -> prov_pb.ProvenanceRef:
    return prov_pb.ProvenanceRef(community=prov_pb.CommunityRef(id=cid))


def _number(metric: str, value: float) -> prov_pb.ProvenanceRef:
    return prov_pb.ProvenanceRef(
        number=prov_pb.NumberRef(metric=metric, value=value)
    )


def _edge(eid: str, src: int, dst: int) -> prov_pb.ProvenanceRef:
    return prov_pb.ProvenanceRef(edge=prov_pb.EdgeRef(id=eid, src=src, dst=dst))


def _time_range(from_s: int, to_s: int) -> prov_pb.ProvenanceRef:
    return prov_pb.ProvenanceRef(
        time_range=prov_pb.TimeRangeRef(from_s=from_s, to_s=to_s)
    )


def _store_with_wallet_profile_output() -> PrimitiveBindingStore:
    """Realistic wallet_profile output. Mirror of the Rust test helper."""
    value_json = {
        "addr": "9XYZ",
        "stats": {
            "total_volume_lamports": 12.4,
            "degree": 33,
            "in_volume_lamports": 8.2,
            "out_volume_lamports": 4.2,
        },
        "top_counterparties": [
            {"addr": "ABC", "volume": 3.1},
            {"addr": "DEF", "volume": 2.5},
        ],
        "community_id": 7,
    }
    provenance = [
        _wallet("9XYZ", idx=1),
        _wallet("ABC"),
        _community(7),
        _number("volume", 12.4),
        _number("degree", 33.0),
    ]
    binding = build_binding(
        primitive="wallet_profile",
        call_id="wallet_profile:01H",
        captured_at_ms=0,
        value_json=value_json,
        provenance=provenance,
    )
    store = PrimitiveBindingStore()
    store.record(binding)
    return store


def test_approves_when_all_refs_trace():
    store = _store_with_wallet_profile_output()
    provenance = [
        _wallet("9XYZ", idx=1),
        _number("volume", 12.4),
        _number("degree", 33.0),
    ]
    assert verify_chip_values(provenance, store) is None


def test_approves_within_tolerance():
    store = _store_with_wallet_profile_output()
    # 10% default tolerance; 12.4 → 12.5 is well within.
    provenance = [_number("volume", 12.5)]
    assert verify_chip_values(provenance, store) is None


def test_retracts_outside_tolerance():
    store = _store_with_wallet_profile_output()
    provenance = [_number("volume", 50.0)]
    err = verify_chip_values(provenance, store)
    assert err is not None
    assert err.kind == "number_not_in_binding"
    assert err.metric == "volume"
    assert err.value == 50.0


def test_retracts_unsourced_wallet():
    store = _store_with_wallet_profile_output()
    provenance = [_wallet("FAKE_WALLET_NEVER_SEEN")]
    err = verify_chip_values(provenance, store)
    assert err is not None
    assert err.kind == "wallet_not_in_binding"
    assert err.addr == "FAKE_WALLET_NEVER_SEEN"


def test_retracts_unsourced_community():
    store = _store_with_wallet_profile_output()
    provenance = [_community(9999)]
    err = verify_chip_values(provenance, store)
    assert err is not None
    assert err.kind == "community_not_in_binding"
    assert err.community_id == 9999


def test_empty_provenance_approves():
    store = _store_with_wallet_profile_output()
    assert verify_chip_values([], store) is None


def test_empty_store_with_any_ref_retracts():
    store = PrimitiveBindingStore()
    provenance = [_number("volume", 12.4)]
    err = verify_chip_values(provenance, store)
    assert err is not None
    assert err.kind == "number_not_in_binding"


def test_raw_unit_class_skips_value_check():
    """Metric name like "score" doesn't classify into any known unit;
    don't retract on Raw class. Constitution gate handles judgment on
    unrecognized metrics."""
    store = _store_with_wallet_profile_output()
    provenance = [_number("score", 999_999.0)]
    assert verify_chip_values(provenance, store) is None


def test_edge_and_timerange_skipped():
    store = _store_with_wallet_profile_output()
    provenance = [_edge("1234:1", 1, 2), _time_range(100, 200)]
    assert verify_chip_values(provenance, store) is None


def test_first_mismatch_short_circuits():
    """Multiple bad refs: surface the first and stop. One retract reason
    per turn; same as Rust gate semantics."""
    store = _store_with_wallet_profile_output()
    provenance = [
        _number("volume", 50.0),  # outside tolerance
        _wallet("FAKE"),  # also bad
    ]
    err = verify_chip_values(provenance, store)
    assert err is not None
    assert err.kind == "number_not_in_binding"
    assert err.metric == "volume"
