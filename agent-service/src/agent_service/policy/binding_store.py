"""Primitive-binding store (ship 3). Captures every successful
primitive output during a thread's lifetime so the policy gate can
verify that numbers and entities cited by the model trace back to real
data we returned, not values invented out of whole cloth.

Direct port of `backend/src/agent/primitives/binding_store.rs`. Same
classification rules, same FIFO cap, same number-walk semantics.

Provenance refs come in as the proto `ProvenanceRef` messages directly
from `PrimitiveResult.provenance`; we accept the raw proto objects and
inspect the active oneof case via `WhichOneof("ref")`. Avoids parallel
hand-typed dataclasses for what's already a wire shape.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from multichain.wire.shared.v1 import provenance_pb2

from agent_service.policy.crosscheck import ExtractedNumber, UnitClass

# FIFO cap on per-thread bindings. 64 covers tens of turns of typical
# dogfood without unbounded growth. Tunable; matches the Rust constant.
MAX_THREAD_BINDINGS: int = 64


@dataclass(slots=True)
class BindingEntities:
    """Wallets, communities, time ranges declared in this binding's
    provenance. Flattened for fast set membership checks during the
    claim provenance-ref validation step."""

    wallets: set[str] = field(default_factory=set)
    communities: set[int] = field(default_factory=set)
    has_time_range: bool = False


@dataclass(slots=True)
class PrimitiveBinding:
    """One captured primitive output. Built immediately after a
    successful dispatch and pushed into the thread's store."""

    call_id: str
    primitive: str
    captured_at_ms: int
    provenance: list[provenance_pb2.ProvenanceRef]
    numbers: list[ExtractedNumber]
    entities: BindingEntities


class PrimitiveBindingStore:
    """Per-thread ring buffer of primitive bindings. Cheap to clone
    by reference; the only consumer outside the loop is the policy
    gate, which holds a reference, not a clone. Eviction is FIFO at
    `MAX_THREAD_BINDINGS`."""

    def __init__(self) -> None:
        self._bindings: deque[PrimitiveBinding] = deque()

    def record(self, binding: PrimitiveBinding) -> None:
        """Append a binding. Evicts the oldest when the store overflows."""
        self._bindings.append(binding)
        while len(self._bindings) > MAX_THREAD_BINDINGS:
            self._bindings.popleft()

    def __len__(self) -> int:
        return len(self._bindings)

    def is_empty(self) -> bool:
        return not self._bindings

    def __iter__(self) -> Iterable[PrimitiveBinding]:
        return iter(self._bindings)

    def all_numbers(self) -> list[ExtractedNumber]:
        """Flat list of every cross-check-able number across all bindings."""
        out: list[ExtractedNumber] = []
        for b in self._bindings:
            out.extend(b.numbers)
        return out

    def all_wallets(self) -> set[str]:
        out: set[str] = set()
        for b in self._bindings:
            out.update(b.entities.wallets)
        return out

    def all_communities(self) -> set[int]:
        out: set[int] = set()
        for b in self._bindings:
            out.update(b.entities.communities)
        return out

    def has_any_time_range(self) -> bool:
        return any(b.entities.has_time_range for b in self._bindings)

    def call_ids(self) -> list[str]:
        """Concatenate every binding's call_id in chronological order.
        Useful for "what primitives did this turn rely on" queries
        from spans or eval probes."""
        return [b.call_id for b in self._bindings]


def build_binding(
    primitive: str,
    call_id: str,
    captured_at_ms: int,
    value_json: dict | list | float | int | str | None,
    provenance: list[provenance_pb2.ProvenanceRef],
) -> PrimitiveBinding:
    """Build a `PrimitiveBinding` from a primitive's dispatch output.
    `value_json` is walked for numbers; `provenance` is walked for
    entities. Both walks are deterministic and synchronous."""
    numbers: list[ExtractedNumber] = []
    _walk_numbers("", value_json, numbers)

    # Provenance also carries explicit Number refs with a metric string.
    # Fold into the same number set so chip-value compares can match
    # against either source.
    for r in provenance:
        case = r.WhichOneof("ref")
        if case == "number":
            num = r.number
            numbers.append(
                ExtractedNumber(
                    value=num.value,
                    unit_class=_classify_field_name(num.metric),
                    hedged=False,
                )
            )

    return PrimitiveBinding(
        call_id=call_id,
        primitive=primitive,
        captured_at_ms=captured_at_ms,
        provenance=list(provenance),
        numbers=numbers,
        entities=_collect_entities(provenance),
    )


def _walk_numbers(
    field_path: str,
    v: Any,
    out: list[ExtractedNumber],
) -> None:
    """Recursively walk a JSON value, classifying each numeric leaf by
    its field-name path. Arrays inherit their parent field name (so
    `top_wallets[0].volume` classifies on `volume`). Objects descend
    keyed by field name."""
    # bool is a subclass of int in Python; exclude before the numeric branch.
    if isinstance(v, bool):
        return
    if isinstance(v, (int, float)):
        unit_class = _classify_field_name(field_path)
        out.append(ExtractedNumber(value=float(v), unit_class=unit_class, hedged=False))
        return
    if isinstance(v, list):
        for item in v:
            _walk_numbers(field_path, item, out)
        return
    if isinstance(v, dict):
        for k, child in v.items():
            _walk_numbers(k, child, out)


def _classify_field_name(name: str) -> UnitClass:
    """Map a field name to a unit class. Conservative: unrecognized
    names go to `RAW`. Mirror of `policy_crosscheck.classify_metric`
    with one nuance: `community_id` resolves to CommunityId before
    the count-suffix check (`id` could otherwise pull it to Raw)."""
    lower = name.lower()
    if lower == "community_id" or "community_id" in lower:
        return UnitClass.COMMUNITY_ID
    if any(
        token in lower
        for token in ("sol", "lamport", "volume", "inflow", "outflow", "inbound", "outbound")
    ):
        return UnitClass.SOL
    if any(
        token in lower
        for token in (
            "count",
            "degree",
            "size",
            "connection",
            "edges",
            "edge_count",
            "counterparty",
            "counterparties",
            "nodes",
        )
    ) or lower == "tx":
        return UnitClass.COUNT
    return UnitClass.RAW


def _collect_entities(provenance: list[provenance_pb2.ProvenanceRef]) -> BindingEntities:
    e = BindingEntities()
    for r in provenance:
        case = r.WhichOneof("ref")
        if case == "wallet":
            e.wallets.add(r.wallet.addr)
        elif case == "community":
            e.communities.add(r.community.id)
        elif case == "time_range":
            e.has_time_range = True
        # edge / number variants don't add entities; numbers already collected.
    return e
