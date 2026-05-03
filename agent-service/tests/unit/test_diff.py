"""Parity tests for the deterministic diff walker. Mirror of
`backend/src/agent/diff.rs` test set. Inputs are plain Python dicts
(matching what `PrimitiveResult.value` produces); outputs are proto
`Delta` messages."""

from __future__ import annotations

from agent_service.diff import FieldKind, diff_outputs


def test_unchanged_within_tolerance_counts_as_unchanged():
    prior = {"vol": 100.0}
    current = {"vol": 105.0}  # +5% < 10% tolerance
    spec = [("vol", FieldKind.number())]
    d = diff_outputs("p", spec, prior, current)
    assert len(d.changed) == 0
    assert d.unchanged_field_count == 1


def test_outside_tolerance_produces_number_moved():
    prior = {"vol": 100.0}
    current = {"vol": 120.0}  # +20% > 10%
    spec = [("vol", FieldKind.number())]
    d = diff_outputs("wallet_profile", spec, prior, current)
    assert len(d.changed) == 1
    fc = d.changed[0].change
    assert fc.WhichOneof("change") == "number_moved"
    assert fc.number_moved.prior == 100.0
    assert fc.number_moved.current == 120.0
    assert abs(fc.number_moved.pct - 0.20) < 1e-9
    assert d.changed[0].primitive == "wallet_profile"
    assert d.changed[0].field_path == "vol"
    assert d.unchanged_field_count == 0


def test_nested_path_walks_correctly():
    prior = {"stats": {"in_volume_lamports": 10.0, "out_volume_lamports": 5.0}}
    current = {"stats": {"in_volume_lamports": 10.5, "out_volume_lamports": 5.0}}
    spec = [
        ("stats.in_volume_lamports", FieldKind.number()),
        ("stats.out_volume_lamports", FieldKind.number()),
    ]
    d = diff_outputs("p", spec, prior, current)
    # in_volume moved 5% within 10% tol; out_volume exactly equal.
    assert len(d.changed) == 0
    assert d.unchanged_field_count == 2


def test_count_kind_any_delta_changed():
    prior = {"edge_count": 31}
    current = {"edge_count": 32}
    spec = [("edge_count", FieldKind.count())]
    d = diff_outputs("p", spec, prior, current)
    assert len(d.changed) == 1
    fc = d.changed[0].change
    assert fc.WhichOneof("change") == "count_changed"
    assert fc.count_changed.prior == 31.0
    assert fc.count_changed.current == 32.0


def test_entity_set_added_removed():
    prior = {"top": [{"addr": "A"}, {"addr": "B"}, {"addr": "C"}]}
    current = {"top": [{"addr": "A"}, {"addr": "B"}, {"addr": "D"}]}
    spec = [("top", FieldKind.entity_set("addr"))]
    d = diff_outputs("p", spec, prior, current)
    assert len(d.changed) == 1
    fc = d.changed[0].change
    assert fc.WhichOneof("change") == "set_changed"
    assert list(fc.set_changed.added) == ["D"]
    assert list(fc.set_changed.removed) == ["C"]


def test_entity_set_unchanged_when_only_order_differs():
    prior = {"top": [{"addr": "A"}, {"addr": "B"}]}
    current = {"top": [{"addr": "B"}, {"addr": "A"}]}
    spec = [("top", FieldKind.entity_set("addr"))]
    d = diff_outputs("p", spec, prior, current)
    assert len(d.changed) == 0
    assert d.unchanged_field_count == 1


def test_ignore_kind_skipped():
    prior = {"timestamp": 100, "vol": 50.0}
    current = {"timestamp": 200, "vol": 50.0}
    spec = [("timestamp", FieldKind.ignore()), ("vol", FieldKind.number())]
    d = diff_outputs("p", spec, prior, current)
    assert len(d.changed) == 0
    assert d.unchanged_field_count == 2


def test_missing_field_treated_as_changed_for_number_kind():
    prior = {"vol": 100.0}
    current = {}
    spec = [("vol", FieldKind.number())]
    d = diff_outputs("p", spec, prior, current)
    assert len(d.changed) == 1


def test_zero_prior_with_nonzero_current_is_changed():
    prior = {"vol": 0.0}
    current = {"vol": 5.0}
    spec = [("vol", FieldKind.number())]
    d = diff_outputs("p", spec, prior, current)
    assert len(d.changed) == 1
    fc = d.changed[0].change
    assert fc.WhichOneof("change") == "number_moved"
    assert fc.number_moved.prior == 0.0
    assert fc.number_moved.current == 5.0
    # pct safely 0 when prior is 0 (avoid div-by-zero).
    assert fc.number_moved.pct == 0.0


def test_empty_set_to_populated_set_is_changed():
    prior = {"top": []}
    current = {"top": [{"addr": "X"}]}
    spec = [("top", FieldKind.entity_set("addr"))]
    d = diff_outputs("p", spec, prior, current)
    assert len(d.changed) == 1
    fc = d.changed[0].change
    assert list(fc.set_changed.added) == ["X"]
    assert list(fc.set_changed.removed) == []


def test_all_unchanged_returns_empty_changed():
    prior = {"size": 8, "vol": 100.0, "members": [{"addr": "A"}]}
    current = {"size": 8, "vol": 102.0, "members": [{"addr": "A"}]}  # +2% within 10%
    spec = [
        ("size", FieldKind.count()),
        ("vol", FieldKind.number()),
        ("members", FieldKind.entity_set("addr")),
    ]
    d = diff_outputs("community_summary", spec, prior, current)
    assert len(d.changed) == 0
    assert d.unchanged_field_count == 3
