"""Ship 5a structural value compare. Walks a provenance array and
verifies every entry traces back to the binding store.

Direct port of `backend/src/agent/policy_structural.rs`. Same
short-circuit semantics, same Edge/TimeRange skip, same Raw-class
shortcut. Operates over proto `ProvenanceRef` messages directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .binding_store import PrimitiveBindingStore
from .crosscheck import CrosscheckConfig, UnitClass, classify_metric, within_tolerance


@dataclass(frozen=True, slots=True)
class MismatchError:
    """Reason a structural compare failed. The retract message surfaces
    this on the SSE wire and into the ledger so retries can name the
    specific entry that didn't trace.

    Three variants share the dataclass. `kind` selects, the other
    fields populate per kind (number / wallet / community)."""

    kind: str  # "number_not_in_binding" | "wallet_not_in_binding" | "community_not_in_binding"
    metric: str = ""
    value: float = 0.0
    unit_class: UnitClass = UnitClass.RAW
    addr: str = ""
    community_id: int = 0

    def to_human_string(self) -> str:
        if self.kind == "number_not_in_binding":
            unit = {
                UnitClass.SOL: " SOL",
                UnitClass.COUNT: "",
                UnitClass.COMMUNITY_ID: " (community id)",
                UnitClass.RAW: "",
            }[self.unit_class]
            return (
                f"cited number {self.metric}={_fmt_value(self.value)}{unit} "
                "does not trace to any primitive output"
            )
        if self.kind == "wallet_not_in_binding":
            return (
                f"cited wallet {self.addr} was not returned by any primitive "
                "call this thread"
            )
        if self.kind == "community_not_in_binding":
            return (
                f"cited community {self.community_id} was not returned by any "
                "primitive call this thread"
            )
        return f"unknown structural error: {self.kind}"


def verify_chip_values(
    provenance: list[Any],
    binding: PrimitiveBindingStore,
) -> MismatchError | None:
    """Walk the provenance array and verify every entry traces. Returns
    `None` on full match, the first `MismatchError` otherwise.

    Empty provenance approves; the caller (claim leg / narrative leg)
    enforces "must have provenance" separately. Empty binding store with
    non-empty provenance always errors on the first Number/Wallet/
    Community ref since nothing matches; that's the desired semantic.

    `Edge` and `TimeRange` provenance variants are not validated here:
    today's binding store doesn't carry edge ids, and `TimeRange`
    arrives in ship 5b's warehouse primitives."""
    cfg = CrosscheckConfig()
    store_numbers = binding.all_numbers()
    store_wallets = binding.all_wallets()
    store_communities = binding.all_communities()

    for prov in provenance:
        case = prov.WhichOneof("ref")
        if case == "number":
            num = prov.number
            unit_class = classify_metric(num.metric)
            # Raw class always approves: the metric name didn't map to a
            # known unit, so we can't typed-compare. Conservative: don't
            # retract on something we don't recognize, the constitution
            # gate carries the judgment.
            if unit_class is UnitClass.RAW:
                continue
            matched = any(
                src.unit_class is unit_class
                and within_tolerance(num.value, src.value, cfg.declarative_tolerance)
                for src in store_numbers
            )
            if not matched:
                return MismatchError(
                    kind="number_not_in_binding",
                    metric=num.metric,
                    value=num.value,
                    unit_class=unit_class,
                )
        elif case == "wallet":
            addr = prov.wallet.addr
            if addr not in store_wallets:
                return MismatchError(kind="wallet_not_in_binding", addr=addr)
        elif case == "community":
            cid = prov.community.id
            if cid not in store_communities:
                return MismatchError(kind="community_not_in_binding", community_id=cid)
        # edge / time_range: skip per fn doc.

    return None


def _fmt_value(v: float) -> str:
    if v.is_integer() and abs(v) < 1e15:
        return f"{v:.0f}"
    if abs(v) >= 1.0:
        return f"{v:.2f}"
    return f"{v:.4f}"
