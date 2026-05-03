"""Ship 4 deterministic diff walker. Operates over typed primitive
outputs (already plain Python dicts from `PrimitiveResult.value`)
using a per-primitive `diff_spec` that declares each field's
comparison strategy. Produces a typed `Delta` proto the loop hands to
the narrative-on-delta call (or short-circuits on empty).

Direct port of `backend/src/agent/diff.rs`. Same numeric tolerance
semantics (reuses `policy.crosscheck.within_tolerance`), same
EntitySet membership logic, same shape-drift fallback (missing field
treated as changed for Number kind).

Diff spec entries are `(field_path: str, kind: FieldKind)` tuples.
`field_path` supports dotted access (`stats.in_volume_lamports` walks
`obj['stats']['in_volume_lamports']`).

Output: `multichain.wire.agent.v1.Delta` proto messages so the loop
driver can stuff them straight onto the SSE wire.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from multichain.wire.agent.v1 import diff_pb2

from .policy.crosscheck import within_tolerance

# Default tolerance for numeric fields when the per-class spec doesn't
# override. Matches `CrosscheckConfig.declarative_tolerance`. Hedging
# never applies on the diff path (we're comparing primitive outputs,
# not narrative prose).
DEFAULT_NUMBER_TOLERANCE: float = 0.10


class FieldKindTag(str, Enum):
    NUMBER = "number"
    ENTITY_SET = "entity_set"
    COUNT = "count"
    IGNORE = "ignore"


@dataclass(frozen=True, slots=True)
class FieldKind:
    """Comparison strategy for a single primitive output field. Per-
    primitive `diff_spec` returns `(field_path, FieldKind)` tuples; the
    walker dispatches on `tag`.

    - NUMBER:     tolerance compare via `within_tolerance`
    - ENTITY_SET: set-membership compare for arrays of objects keyed by `key`
    - COUNT:      any delta is meaningful (integer fields)
    - IGNORE:     skip; counts as unchanged from the model's perspective"""

    tag: FieldKindTag
    tolerance: float = DEFAULT_NUMBER_TOLERANCE
    key: str = ""

    @classmethod
    def number(cls, tolerance: float = DEFAULT_NUMBER_TOLERANCE) -> "FieldKind":
        return cls(tag=FieldKindTag.NUMBER, tolerance=tolerance)

    @classmethod
    def entity_set(cls, key: str) -> "FieldKind":
        return cls(tag=FieldKindTag.ENTITY_SET, key=key)

    @classmethod
    def count(cls) -> "FieldKind":
        return cls(tag=FieldKindTag.COUNT)

    @classmethod
    def ignore(cls) -> "FieldKind":
        return cls(tag=FieldKindTag.IGNORE)


def diff_outputs(
    primitive_name: str,
    spec: list[tuple[str, FieldKind]],
    prior: dict | None,
    current: dict | None,
) -> diff_pb2.Delta:
    """Walk both serialized outputs against the spec, building a `Delta`.
    Each spec entry produces at most one `FieldDelta` (when the field
    changed) or contributes to `unchanged_field_count` (when it didn't,
    or when its kind is `IGNORE`).

    Robust to missing fields: a field path absent in either dict is
    reported as changed for Number/Count/EntitySet kinds (best-effort
    signal that something shifted shape-wise) UNLESS its kind is IGNORE."""
    changed: list[diff_pb2.FieldDelta] = []
    unchanged: int = 0

    for field_path, kind in spec:
        prior_val = _pointer_lookup(prior, field_path)
        current_val = _pointer_lookup(current, field_path)

        if kind.tag is FieldKindTag.IGNORE:
            unchanged += 1
            continue

        if kind.tag is FieldKindTag.NUMBER:
            p = _as_float(prior_val)
            c = _as_float(current_val)
            if p is not None and c is not None:
                if within_tolerance(c, p, kind.tolerance):
                    unchanged += 1
                else:
                    pct = 0.0 if p == 0.0 else (c - p) / p
                    changed.append(
                        diff_pb2.FieldDelta(
                            field_path=field_path,
                            primitive=primitive_name,
                            change=diff_pb2.FieldChange(
                                number_moved=diff_pb2.FieldChangeNumberMoved(
                                    prior=p, current=c, pct=pct
                                )
                            ),
                        )
                    )
            else:
                # Missing on either side or non-numeric: flag as changed
                # so we don't silently swallow shape drift.
                changed.append(
                    diff_pb2.FieldDelta(
                        field_path=field_path,
                        primitive=primitive_name,
                        change=diff_pb2.FieldChange(
                            number_moved=diff_pb2.FieldChangeNumberMoved(
                                prior=p if p is not None else 0.0,
                                current=c if c is not None else 0.0,
                                pct=0.0,
                            )
                        ),
                    )
                )
            continue

        if kind.tag is FieldKindTag.COUNT:
            p = _as_float(prior_val)
            c = _as_float(current_val)
            same = (p is not None and c is not None and p == c)
            if same:
                unchanged += 1
            else:
                changed.append(
                    diff_pb2.FieldDelta(
                        field_path=field_path,
                        primitive=primitive_name,
                        change=diff_pb2.FieldChange(
                            count_changed=diff_pb2.FieldChangeCountChanged(
                                prior=p if p is not None else 0.0,
                                current=c if c is not None else 0.0,
                            )
                        ),
                    )
                )
            continue

        if kind.tag is FieldKindTag.ENTITY_SET:
            prior_set = _collect_keys(prior_val, kind.key)
            current_set = _collect_keys(current_val, kind.key)
            added = sorted(current_set - prior_set)
            removed = sorted(prior_set - current_set)
            if not added and not removed:
                unchanged += 1
            else:
                changed.append(
                    diff_pb2.FieldDelta(
                        field_path=field_path,
                        primitive=primitive_name,
                        change=diff_pb2.FieldChange(
                            set_changed=diff_pb2.FieldChangeSetChanged(
                                added=added, removed=removed
                            )
                        ),
                    )
                )
            continue

    return diff_pb2.Delta(changed=changed, unchanged_field_count=unchanged)


def _pointer_lookup(v: Any, dotted: str) -> Any:
    """Look up a dotted field path against a dict. Supports nested
    objects (`stats.in_volume_lamports` walks
    `obj['stats']['in_volume_lamports']`). Arrays are not indexable
    via dotted paths; spec entries that need array values point at the
    array itself."""
    if v is None:
        return None
    cur: Any = v
    for seg in dotted.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return None
    return cur


def _as_float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _collect_keys(v: Any, key: str) -> set[str]:
    """Extract per-element keys from a list of objects. Returns a set so
    the diff is order-insensitive. Non-list or missing values produce an
    empty set."""
    if not isinstance(v, list):
        return set()
    out: set[str] = set()
    for elem in v:
        if isinstance(elem, dict) and key in elem:
            val = elem[key]
            if isinstance(val, str):
                out.add(val)
            elif isinstance(val, (int, float)) and not isinstance(val, bool):
                out.add(str(val))
    return out


# ---------------------------------------------------------------------------
# Per-primitive diff specs. Mirrors the per-primitive
# `diff_spec()` in the Rust primitives. wallet_profile + community_summary
# are the only two with real diff_spec today; emit_claim has empty spec
# and never replays anyway.
# ---------------------------------------------------------------------------


WALLET_PROFILE_DIFF_SPEC: list[tuple[str, FieldKind]] = [
    ("stats.in_volume_lamports", FieldKind.number()),
    ("stats.out_volume_lamports", FieldKind.number()),
    ("stats.bidir_volume_lamports", FieldKind.number()),
    ("stats.total_volume_lamports", FieldKind.number()),
    ("stats.degree", FieldKind.count()),
    ("community_id", FieldKind.count()),
    ("top_counterparties", FieldKind.entity_set(key="addr")),
]

COMMUNITY_SUMMARY_DIFF_SPEC: list[tuple[str, FieldKind]] = [
    ("size", FieldKind.count()),
    ("internal_volume", FieldKind.number()),
    ("external_volume", FieldKind.number()),
    ("top_members", FieldKind.entity_set(key="addr")),
]


def spec_for(primitive_name: str) -> list[tuple[str, FieldKind]]:
    """Lookup table from primitive name to diff spec. Returns empty
    list for primitives without a meaningful spec (e.g. `emit_claim`)."""
    if primitive_name == "wallet_profile":
        return WALLET_PROFILE_DIFF_SPEC
    if primitive_name == "community_summary":
        return COMMUNITY_SUMMARY_DIFF_SPEC
    return []
