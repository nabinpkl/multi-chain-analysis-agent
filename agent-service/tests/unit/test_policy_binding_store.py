"""Parity tests for the binding store. Mirror of `binding_store.rs`'s
test set."""

from __future__ import annotations

from agent_service.policy.binding_store import (
    MAX_THREAD_BINDINGS,
    BindingEntities,
    PrimitiveBinding,
    PrimitiveBindingStore,
    _classify_field_name,
    _walk_numbers,
    build_binding,
)
from agent_service.policy.crosscheck import ExtractedNumber, UnitClass
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


def test_classify_field_basics():
    assert _classify_field_name("volume") is UnitClass.SOL
    assert _classify_field_name("total_volume") is UnitClass.SOL
    assert _classify_field_name("internal_volume") is UnitClass.SOL
    assert _classify_field_name("sol_inflow") is UnitClass.SOL
    assert _classify_field_name("degree") is UnitClass.COUNT
    assert _classify_field_name("sol_degree") is UnitClass.SOL
    assert _classify_field_name("size") is UnitClass.COUNT
    assert _classify_field_name("edge_count") is UnitClass.COUNT
    assert _classify_field_name("community_id") is UnitClass.COMMUNITY_ID
    assert _classify_field_name("age_in_window_secs") is UnitClass.RAW


def test_walk_numbers_flat_object():
    v = {"degree": 33, "volume": 12.4, "community_id": 42, "age_in_window_secs": 50}
    out: list[ExtractedNumber] = []
    _walk_numbers("", v, out)
    assert len(out) == 4
    counts = sum(1 for n in out if n.unit_class is UnitClass.COUNT)
    sols = sum(1 for n in out if n.unit_class is UnitClass.SOL)
    cids = sum(1 for n in out if n.unit_class is UnitClass.COMMUNITY_ID)
    assert counts == 1
    assert sols == 1
    assert cids == 1


def test_walk_numbers_nested_array_inherits_field_name():
    """Numbers under `top_wallets[].volume` classify as Sol because the
    immediate parent key is `volume`. Walker descends into objects keyed
    by field name; array layer carries parent name through to items."""
    v = {
        "top_wallets": [
            {"addr": "AAA", "volume": 5.0, "degree": 7},
            {"addr": "BBB", "volume": 3.0, "degree": 4},
        ]
    }
    out: list[ExtractedNumber] = []
    _walk_numbers("", v, out)
    sols = [n for n in out if n.unit_class is UnitClass.SOL]
    counts = [n for n in out if n.unit_class is UnitClass.COUNT]
    assert len(sols) == 2, out
    assert len(counts) == 2


def test_build_binding_collects_provenance_entities():
    provenance = [
        _wallet("AAA", idx=0),
        _wallet("BBB", idx=1),
        _community(42),
        _number("degree", 33.0),
    ]
    binding = build_binding(
        primitive="wallet_profile",
        call_id="wallet_profile:01HXY",
        captured_at_ms=123,
        value_json={"degree": 33, "volume": 12.4},
        provenance=provenance,
    )
    assert "AAA" in binding.entities.wallets
    assert "BBB" in binding.entities.wallets
    assert 42 in binding.entities.communities
    # Numbers from JSON walk (degree=Count, volume=Sol) +
    # provenance Number entry (degree -> Count). Total = 3.
    assert len(binding.numbers) == 3


def test_store_record_evicts_at_cap():
    store = PrimitiveBindingStore()
    for i in range(MAX_THREAD_BINDINGS + 5):
        store.record(
            PrimitiveBinding(
                call_id=f"p:{i}",
                primitive="wallet_profile",
                captured_at_ms=i,
                provenance=[],
                numbers=[],
                entities=BindingEntities(),
            )
        )
    assert len(store) == MAX_THREAD_BINDINGS
    # First 5 should have been evicted; oldest survivor is index 5.
    first = next(iter(store))
    assert first.call_id == "p:5"


def test_store_aggregates_across_bindings():
    store = PrimitiveBindingStore()
    store.record(
        build_binding(
            primitive="wallet_profile",
            call_id="wp:1",
            captured_at_ms=1,
            value_json={"degree": 33, "volume": 12.4},
            provenance=[_wallet("AAA", idx=0)],
        )
    )
    store.record(
        build_binding(
            primitive="community_summary",
            call_id="cs:1",
            captured_at_ms=2,
            value_json={"size": 7, "total_volume": 100.0},
            provenance=[_community(42)],
        )
    )
    nums = store.all_numbers()
    # wp: degree(Count) + volume(Sol) = 2
    # cs: size(Count) + total_volume(Sol) = 2
    assert len(nums) == 4
    assert "AAA" in store.all_wallets()
    assert 42 in store.all_communities()
    assert store.call_ids() == ["wp:1", "cs:1"]
