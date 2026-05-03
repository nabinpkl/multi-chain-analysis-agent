"""Agent-only wire types: SSE frames, claim shapes, gate verdicts.

Hand-written here as the Python source-of-truth. Phase I.6 generates
matching frontend TypeScript via `json-schema-to-typescript` so the
two languages can never drift.

Boundary rules:
- Anything Rust ALSO produces (graph data, primitive I/O, snapshot
  envelopes) lives in `wire/shared/` and is auto-generated from
  Rust schemas via Phase A's pipeline. We import those types here.
- Everything in this file is Python -> Frontend only. Rust never
  touches these shapes; the agent plane owns them end-to-end.

Pydantic config:
- `extra='forbid'` on every model EXCEPT the `ConstitutionVerdict`
  family. Constitution responses come from the policy LLM which
  occasionally adds keys; matching the Rust `serde(default)` lenient
  parser keeps that path forgiving.
- All enums use the kebab-case strings the Rust types serialize
  with (`#[serde(rename_all = "kebab-case")]`).
- Tagged unions use the `discriminator` pattern with a `Field(...,
  discriminator='<tag>')` annotation so pydantic dispatches on the
  same field Rust's serde uses.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# Shared types: imported from the auto-generated Rust -> Python pipeline.
# These are the canonical shapes the Rust data plane produces.
#
# `ClaimKind` and `NumberRef` are defined Rust-side under EmitClaimInput
# (the model's tool-call argument shape) and re-used here so the wire
# Claim frame and the model's tool input share one type identity. If we
# hand-rolled local copies, Phase II's `emit_claim` tool would have to
# convert between two byte-identical-but-class-distinct types.
from .shared import ClaimKind, NumberRef, ProvenanceRef, SubgraphSlice


# ---------------------------------------------------------------------------
# Base config classes
# ---------------------------------------------------------------------------


class _StrictModel(BaseModel):
    """Forbid extra fields. Catches drift between this file and the
    Rust source it mirrors."""

    model_config = ConfigDict(extra="forbid")


class _LenientModel(BaseModel):
    """Ignore extra fields. Used only for ConstitutionVerdict and
    its nested types because the policy LLM is the upstream and
    matching Rust's `serde(default)` lenient parser is the contract."""

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Agent-only enums (kebab-case wire values matching Rust serde)
#
# `ClaimKind` and `NumberRef` are re-exported from `wire.shared` (Rust
# source). The two below are agent-only metadata flags Rust doesn't
# emit on the wire today; defined here so future Python-source frames
# can use them without round-tripping through the codegen pipeline.
# ---------------------------------------------------------------------------


class CostClass(StrEnum):
    cheap = "cheap"
    moderate = "moderate"
    expensive = "expensive"


class DataSource(StrEnum):
    live = "live"
    warehouse = "warehouse"
    external = "external"


# ---------------------------------------------------------------------------
# Stub provenance + small leaf types
# ---------------------------------------------------------------------------


class StubMarker(_StrictModel):
    """Per-claim badge: which stubs short-circuited during this claim's
    emission. Persists into history so stub provenance survives even
    after the global registry is cleared."""

    name: str
    reason: str
    promoted_in_ship: int = Field(ge=0, le=255)


class TimeRangeWire(_StrictModel):
    from_s: int = Field(ge=0)
    to_s: int = Field(ge=0)


# ---------------------------------------------------------------------------
# Tagged unions: PolicyVerdict, PathState, FieldChange, EntityRef
# ---------------------------------------------------------------------------


class PolicyVerdictApproved(_StrictModel):
    verdict: Literal["approved"] = "approved"


class PolicyVerdictRetracted(_StrictModel):
    verdict: Literal["retracted"] = "retracted"
    reason: str


PolicyVerdict = Annotated[
    PolicyVerdictApproved | PolicyVerdictRetracted,
    Field(discriminator="verdict"),
]


class PathStateApproved(_StrictModel):
    state: Literal["approved"] = "approved"


class PathStateRetracted(_StrictModel):
    state: Literal["retracted"] = "retracted"
    reason: str


class PathStateNotApplicable(_StrictModel):
    state: Literal["not_applicable"] = "not_applicable"
    detail: str


PathState = Annotated[
    PathStateApproved | PathStateRetracted | PathStateNotApplicable,
    Field(discriminator="state"),
]


class FieldChangeNumberMoved(_StrictModel):
    """Numeric field outside per-class tolerance. `pct` is the signed
    percent change; 0.0 when prior is 0."""

    kind: Literal["number_moved"] = "number_moved"
    prior: float
    current: float
    pct: float


class FieldChangeSetChanged(_StrictModel):
    """Entity-list field where membership changed. `added`/`removed`
    carry the keys (typically wallet addresses)."""

    kind: Literal["set_changed"] = "set_changed"
    added: list[str]
    removed: list[str]


class FieldChangeCountChanged(_StrictModel):
    """Count-class field where any delta is meaningful."""

    kind: Literal["count_changed"] = "count_changed"
    prior: float
    current: float


FieldChange = Annotated[
    FieldChangeNumberMoved | FieldChangeSetChanged | FieldChangeCountChanged,
    Field(discriminator="kind"),
]


class EntityRefWallet(_StrictModel):
    kind: Literal["wallet"] = "wallet"
    id: str


class EntityRefEdge(_StrictModel):
    kind: Literal["edge"] = "edge"
    id: str


class EntityRefCommunity(_StrictModel):
    kind: Literal["community"] = "community"
    id: int = Field(ge=0)


EntityRef = Annotated[
    EntityRefWallet | EntityRefEdge | EntityRefCommunity,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Subgraph slice composition (NodeSummary, EdgeSummary)
# ---------------------------------------------------------------------------


class NodeSummary(_StrictModel):
    """Subgraph node row. `role` is None when unclassified."""

    addr: str
    role: str | None = None


class EdgeSummary(_StrictModel):
    """Subgraph edge row. `volume` in lamports."""

    src: str
    dst: str
    volume: float


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


class Claim(_StrictModel):
    """Streamed analytical statement. The body uses `${ref:N}`
    placeholders the frontend replaces with interactive chips at
    render time. `provenance[N]` is the typed entry the chip
    resolves against.

    Wire shape locked to match Rust `agent::types::Claim` byte-for-byte.
    Drift surfaces as failed Phase I.5 SSE golden parse tests."""

    id: str = Field(description="ULID, sortable by emission order.")
    session_id: str
    kind: ClaimKind
    headline: str = Field(description="One-line plaintext headline.")
    body_markdown: str = Field(
        description="Structured paragraph; ${ref:N} placeholders -> provenance chips.",
    )
    provenance: list[ProvenanceRef]
    support_numbers: list[NumberRef]
    subgraph_slice: SubgraphSlice | None = None
    policy_verdict: PolicyVerdict
    stubs_active: list[StubMarker]
    emitted_at_ms: int = Field(
        ge=0,
        description="Wallclock ms since session started. u32 in Rust.",
    )


# ---------------------------------------------------------------------------
# Narrative channel
# ---------------------------------------------------------------------------


class NarrativeWithRefs(_StrictModel):
    """Approved free-form prose. May contain inline `${ref:N}` tokens
    the renderer resolves against `provenance` (assembled by the loop
    from this turn's emitted Claims, concatenated provenance arrays
    in emission order)."""

    text: str
    provenance: list[ProvenanceRef]


class NarrativeRetracted(_StrictModel):
    """Narrative the constitution gate retracted. Carries the original
    text alongside a friendly user-facing `reason`. `debug_reason`
    only populated when AGENT_DEBUG_PUBLIC=1 (dev-mode); absent in
    prod so the wire stays sterile."""

    text: str
    reason: str
    debug_reason: str | None = None


# ---------------------------------------------------------------------------
# Gate path (builder-view trace, ship 3.5)
# ---------------------------------------------------------------------------


class PathStep(_StrictModel):
    """One step in the gate's execution path. Stage is a dotted id
    (e.g. `claim.stay_in_role`, `narrative.cross_check.paraphrase_aware_match`).
    `elapsed_us` is wallclock microseconds; ordering only, determinism
    not promised."""

    stage: str
    state: PathState
    elapsed_us: int = Field(ge=0)
    note: str = Field(description="Single-line note: what was checked, what verdict.")


class GatePath(_StrictModel):
    """Full path of one channel's gate run. Emitted as `GatePath` SSE
    frame when `AgentRequest.show_trace=true`. Trace is always built
    and ledgered; the frame is wire-only."""

    channel: str
    switches: AgentSwitches  # forward ref; resolved below
    steps: list[PathStep]
    final_verdict: PolicyVerdict


# ---------------------------------------------------------------------------
# Switches (ablation toggles, ship 3.5)
# ---------------------------------------------------------------------------


class CrossCheckSwitches(_StrictModel):
    """`cross_check` sub-modes. Two independent toggles after ship 5a
    retired `text_match`. Both advisory in the strict merge."""

    paraphrase_aware_match: bool = True
    ground_truth_match: bool = False


class AgentSwitches(_StrictModel):
    """Ship 3.5 ablation switches. Each field is a behavior contract;
    when true, the agent has that behavior. Defaults reproduce the
    production preset."""

    stay_in_role: bool = True
    dont_fabricate: bool = True
    cross_check: CrossCheckSwitches = Field(default_factory=CrossCheckSwitches)
    dont_repeat_yourself: bool = True


# Resolve forward ref on GatePath.
GatePath.model_rebuild()


# ---------------------------------------------------------------------------
# Ship 4: incremental answers (delta wire shape)
# ---------------------------------------------------------------------------


class FieldDelta(_StrictModel):
    """One field's change between prior turn's primitive output and the
    freshly re-fetched output. `field_path` is dotted (e.g.
    `stats.in_volume_lamports`). `primitive` is the producing primitive
    name (e.g. `wallet_profile`)."""

    field_path: str
    primitive: str
    change: FieldChange


class Delta(_StrictModel):
    """Full diff result. `unchanged_field_count` powers the builder-view
    chip ('2 changed / 4 unchanged'); only structurally-changed fields
    appear in `changed`."""

    changed: list[FieldDelta]
    unchanged_field_count: int = Field(ge=0)


class NoMovement(_StrictModel):
    """Emitted when `dont_repeat_yourself` fires AND the diff is empty.
    No LLM narrative call happens on this path; the bubble exists so
    the user sees closure ('we covered this in turn N, no movement
    since')."""

    prior_turn: int = Field(ge=0)
    primitives_replayed: list[str]


class ChangedSince(_StrictModel):
    """Emitted when `dont_repeat_yourself` fires AND the diff is
    non-empty. Carries both the typed `Delta` and the small
    narrative call's prose. Frontend can render either."""

    prior_turn: int = Field(ge=0)
    delta: Delta
    prose: str


# ---------------------------------------------------------------------------
# Generic SSE frames
# ---------------------------------------------------------------------------


class Progress(_StrictModel):
    """Lightweight progress ping. Phase + detail are free-form strings
    the frontend can render as a status line."""

    phase: str
    detail: str


class Error(_StrictModel):
    """Terminal turn-level error. The SSE handler renders this as an
    `Error` event before the closing `Done`. `debug_message` only
    populated when AGENT_DEBUG_PUBLIC=1."""

    message: str
    debug_message: str | None = None


# ---------------------------------------------------------------------------
# Constitution gate (lenient parsing, matches Rust serde(default))
# ---------------------------------------------------------------------------


class LlmExtractedNumber(_LenientModel):
    """LLM-side extracted number from constitution gate's `extraction`
    JSON sidecar. `phrase` is debugging context only; surfaces in
    dev-mode debug fields and is discarded during compare."""

    value: float
    unit_class: str
    phrase: str = ""


class LlmExtraction(_LenientModel):
    """Constitution gate's structured sidecar. Numbers the LLM saw in
    narrative + claim text, classified by unit. The structural
    cross-check pairs these against the binding store."""

    narrative_numbers: list[LlmExtractedNumber] = Field(default_factory=list)
    claim_numbers: list[LlmExtractedNumber] = Field(default_factory=list)


class ConstitutionVerdict(_LenientModel):
    """Constitution v3 response shape. `verdict` is one of three
    strings the policy prompt prescribes; `reason` defaults to "" so
    a malformed older-style response still parses cleanly. `extraction`
    is None when the LLM omitted the sidecar entirely."""

    verdict: Literal["approve", "retract", "reject"]
    reason: str = ""
    extraction: LlmExtraction | None = None


# ---------------------------------------------------------------------------
# Inbound: client -> /agent/ask
# ---------------------------------------------------------------------------


class ViewContext(_StrictModel):
    """Structured ground-truth context the frontend builds from its own
    DOM/state. Per D-6 (overview), the context block is the strongest
    disambiguation signal."""

    live_window_secs: int = Field(ge=0)
    focus: EntityRef | None = None
    selection: list[EntityRef] = Field(default_factory=list)


class AgentRequest(_StrictModel):
    """User question + ViewContext. `thread_id` is None on the first
    send of a fresh conversation; the backend mints one and returns
    it. Subsequent follow-ups echo the prior thread_id.

    `switches` defaults to the production preset (everything except
    `ground_truth_match`). `show_trace` controls whether `GatePath`
    frames are emitted; trace is always built and ledgered regardless.

    SECURITY note (mirrored from Rust): switches are reachable from
    any client. Project is a builder portfolio, not a product; we
    explicitly do not hide internals. If this code ever serves real
    end-user traffic, lock the switch surface server-side."""

    user_question: str
    context: ViewContext
    thread_id: str | None = None
    switches: AgentSwitches = Field(default_factory=AgentSwitches)
    show_trace: bool = False


# ---------------------------------------------------------------------------
# Outbound: /agent/ask response + per-turn closer
# ---------------------------------------------------------------------------


class AgentSessionStarted(_StrictModel):
    """Returned synchronously from POST /agent/ask. `session_id` is
    per-turn (drives the SSE GET, ledger row group). `thread_id`
    is the persistent conversation handle. `turn` is 0 on the first
    turn, increments on follow-ups."""

    session_id: str
    thread_id: str
    turn: int = Field(ge=0, default=0)


class AgentDone(_StrictModel):
    """Final SSE event payload. Emitted as the `Done` event by the SSE
    handler. `elapsed_ms` is u32 in Rust (caps at ~50 days)."""

    session_id: str
    elapsed_ms: int = Field(ge=0)
