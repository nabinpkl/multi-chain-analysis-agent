"""Shared taxonomy + tolerance + LLM-extractor compare. Direct port of
`backend/src/agent/policy_crosscheck.rs`. Ship 5a retired the regex-on-
prose machinery; what survives is the typed compare surface used by:

- `policy.binding_store` (numbers walker shares the same `UnitClass`)
- `policy.structural` (chip-value compare uses `within_tolerance` +
  `classify_metric`)
- This module's `cross_check_extracted_pair` (constitution gate's
  paraphrase-aware coherence advisory)

No regex on prose. Same numeric semantics as the Rust path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class UnitClass(str, Enum):
    """Unit class for a parsed number. Compared as equal-class only;
    "12,300 SOL" never matches "12,300 connections" even though the raw
    values agree. `Lamports` and `Sol` collapse to a single class (`Sol`)
    at extraction time so trillion-lamport vs thousand-SOL comparisons
    survive float precision."""

    SOL = "sol"
    COUNT = "count"
    COMMUNITY_ID = "community_id"
    RAW = "raw"


@dataclass(frozen=True, slots=True)
class CrosscheckConfig:
    """Tunable knobs. Defaults match the ship 2.5 plan; iterate via
    dogfood feedback before plumbing as env vars."""

    declarative_tolerance: float = 0.10
    hedged_tolerance: float = 0.15


@dataclass(slots=True)
class ExtractedNumber:
    """One number ready for compare. `value` is the canonical form
    (lamports already divided to SOL; multiplier suffixes already
    expanded). Sources today: LLM extractor sidecar (constitution gate)
    and binding store walking primitive output."""

    value: float
    unit_class: UnitClass
    hedged: bool = False

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "unit_class": self.unit_class.value,
            "hedged": self.hedged,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ExtractedNumber":
        return cls(
            value=float(data["value"]),
            unit_class=UnitClass(data["unit_class"]),
            hedged=bool(data.get("hedged", False)),
        )


@dataclass(frozen=True, slots=True)
class RetractReason:
    """Reason a cross-check retracted. `to_human_string()` produces the
    one-sentence text that flows into the wire `reason` field. Single
    variant today (`unsourced`); kept as a frozen dataclass for future
    discriminated-union extensibility."""

    kind: str  # "unsourced"
    value: float
    unit_class: UnitClass

    def to_human_string(self) -> str:
        if self.kind == "unsourced":
            unit_text = {
                UnitClass.SOL: " SOL",
                UnitClass.COUNT: "",
                UnitClass.COMMUNITY_ID: " (community id)",
                UnitClass.RAW: "",
            }[self.unit_class]
            return (
                f"narrative number {_format_number(self.value)}{unit_text} "
                f"not found in cited Claims"
            )
        return f"unknown retract reason: {self.kind}"


def _format_number(v: float) -> str:
    if v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    return repr(v)


def within_tolerance(narr: float, claim: float, frac: float) -> bool:
    """Pure float compare with fractional tolerance. Same numeric
    semantics as the Rust binding store / structural gate / diff walker."""
    if claim == 0.0:
        return narr == 0.0
    return abs(narr - claim) / abs(claim) <= frac


def classify_metric(metric: str) -> UnitClass:
    """Map a metric string from `support_numbers`, `ProvenanceRef.Number`,
    or a primitive output JSON field name to a `UnitClass`. Conservative:
    unrecognized names go to `RAW` so they don't accidentally satisfy a
    typed claim. Mirror of Rust's `policy_crosscheck::classify_metric`."""
    lower = metric.lower()
    if any(
        token in lower
        for token in ("sol", "lamport", "volume", "inflow", "outflow", "inbound", "outbound")
    ):
        return UnitClass.SOL
    if any(
        token in lower
        for token in ("count", "degree", "connection", "tx", "edge", "node")
    ):
        return UnitClass.COUNT
    if "community" in lower:
        return UnitClass.COMMUNITY_ID
    return UnitClass.RAW


def _has_match(
    n: ExtractedNumber,
    refs: list[ExtractedNumber],
    cfg: CrosscheckConfig,
) -> bool:
    tol = cfg.hedged_tolerance if n.hedged else cfg.declarative_tolerance
    return any(
        r.unit_class == n.unit_class and within_tolerance(n.value, r.value, tol)
        for r in refs
    )


def cross_check_extracted_pair(
    narrative_numbers: list[ExtractedNumber],
    claim_numbers: list[ExtractedNumber],
    extra_source: list[ExtractedNumber],
    config: CrosscheckConfig | None = None,
) -> RetractReason | None:
    """Compare two pre-extracted number sets. Used by the LLM extractor
    path (constitution gate's extraction sidecar) for ship 5a's advisory
    `paraphrase_aware_match` coherence check.

    Returns `None` when every narrative number matches at least one claim
    number OR `extra_source` number on the same `unit_class` within
    tolerance. Returns the first unsourced narrative number on retract.

    Ship 5a note: this is no longer load-bearing for factuality; the
    structural placeholder + chip-value compare carries that role. This
    survives as the coherence advisory under
    `cross_check.paraphrase_aware_match`."""
    cfg = config or CrosscheckConfig()
    if not narrative_numbers:
        return None
    for n in narrative_numbers:
        if not _has_match(n, claim_numbers, cfg) and not _has_match(n, extra_source, cfg):
            return RetractReason(
                kind="unsourced", value=n.value, unit_class=n.unit_class
            )
    return None


@dataclass(slots=True)
class LlmExtractedNumber:
    """LLM-side extracted number, deserialized from the constitution
    gate's `extraction` JSON sidecar. Maps cleanly to `ExtractedNumber`.
    `phrase` is debugging context only; surfaced in dev-mode `debug_*`
    fields and discarded during compare."""

    value: float
    unit_class: str
    phrase: str = ""

    def into_extracted(self) -> ExtractedNumber:
        """Map the string `unit_class` from the LLM into our enum.
        Unknown values fall back to `RAW` so the compare skips them
        rather than silently approving on a misclassification."""
        lower = self.unit_class.lower()
        match lower:
            case "sol":
                uc = UnitClass.SOL
            case "count":
                uc = UnitClass.COUNT
            case "community_id" | "community-id" | "community":
                uc = UnitClass.COMMUNITY_ID
            case _:
                uc = UnitClass.RAW
        # The LLM doesn't tell us hedged-vs-declarative; treat as
        # declarative (tighter tolerance) by default.
        return ExtractedNumber(value=self.value, unit_class=uc, hedged=False)
