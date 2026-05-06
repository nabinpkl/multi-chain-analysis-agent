"""Canonical eval types. Layer 1 of the eval substrate (ADR 14).

These types are the contract every other layer depends on:

- Layer 2 probes consume one typed `*Spec` (e.g. `ClaimGroundedInSpec`),
  return `ProbeResult`.
- Layer 3 runner loads `EvalCase` from YAML, persists `ProbeResult`,
  emits `RunMetadata`.

ADR 14 originally planned a Layer 4 framework adapter on top
(pydantic_evals). The 2026-05-05 ADR addendum reverses that: the
framework's span-querying primitive (`HasMatchingSpan`/`SpanQuery`)
captures spans in-process via a `SimpleSpanProcessor` on the local
TracerProvider, which is incompatible with our cross-process trace
flow (agent → otel-collector → ClickHouse). The adapter would buy
us nothing the probes don't already do. New probe-class additions
(LLM-as-judge, etc.) ride the existing `ProbeKind` Literal +
`probes/<kind>.py` extension point.

The four invariants this layer protects:

1. A case is data, not code (YAML-loadable, stable IDs).
2. A probe is a predicate over an OTel trace (typed spec class per
   kind; one source of truth for probe args).
3. A probe result is a structured artifact (JSON-persistable).
4. The agent under test is invoked exactly the way production
   invokes it (`inputs` is shaped like the production AgentRequest).

Probe specs are a discriminated union over the `kind` field. Each
probe kind has its own `*Spec` pydantic class with its args inlined
as typed fields (no `args: dict[str, Any]` indirection). Benefits:

- One source of truth for probe args. Layer 2 probes consume the
  typed spec class directly; no per-probe re-validation.
- YAML cases fail at load time with a precise error pointing to the
  bad arg, not later at probe-call time.
- IDE/type-checker catches probe-implementation/spec-shape drift.

Rules for evolving this file:

- Adding a probe kind: add a new `*Spec` class, append it to the
  `ProbeSpec` union, register the probe module in `probes/__init__.py`.
  No schema migration of existing YAML cases.
- Adding an arg to an existing probe kind: add a field to that
  probe's `*Spec` class with a default; old YAML cases keep parsing.
- Renaming any field: requires migrating committed YAML cases AND
  baseline JSON files. Avoid.

This module imports nothing from `agent_service` and nothing from
any eval framework. That is load-bearing per ADR 14 (Layer 1 is a
leaf; framework swap touches Layer 4 only).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Probe kinds + framework adapter ids (closed enums)
# ---------------------------------------------------------------------------

# Each value names a probe module under `probes/<value>.py` whose
# `run(spec, trace_id, ch, *, run_id, case_id) -> ProbeResult`
# function implements the predicate. Adding a kind here without
# adding the module is caught by the dispatch import at startup.
ProbeKind = Literal[
    "has_matching_span",
    "tool_called_with_args",
    "claim_grounded_in",
    "gate_passed",
    "span_latency_p50_under",
    "no_span_with_status",
    "llm_call_used_model",
]

# Recorded on every run so cross-run comparisons can detect a swap
# that might have shifted what passes. Only `framework_free` is
# wired today (per ADR 14 2026-05-05 addendum). The Literal stays a
# closed enum; if a future framework earns its keep against our
# probe set, add a value here and a sibling adapter file. YAGNI says
# don't keep dead optionality, so we only carry values that ship.
FrameworkAdapter = Literal["framework_free"]


# ---------------------------------------------------------------------------
# Probe specs (one class per kind; discriminated union below)
# ---------------------------------------------------------------------------


class _ProbeSpecBase(BaseModel):
    """Common shape every probe spec inherits. Holds the discriminator
    field machinery and the probe_id non-empty check. Concrete
    subclasses set `kind` to a literal and add their own typed args."""

    model_config = ConfigDict(extra="forbid")

    probe_id: str = Field(
        description=(
            "Stable per (case, probe). Used as the primary key in "
            "ProbeResult so two probes of the same kind on one case "
            "are distinguishable."
        ),
    )

    @field_validator("probe_id")
    @classmethod
    def _probe_id_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("probe_id must be non-empty")
        return v


class HasMatchingSpanSpec(_ProbeSpecBase):
    """Pass if the trace contains at least one span with `span_name`
    and (if `attrs` is set) all listed attribute key/value pairs."""

    kind: Literal["has_matching_span"] = "has_matching_span"
    span_name: str
    attrs: dict[str, str] = Field(default_factory=dict)


class ToolCalledWithArgsSpec(_ProbeSpecBase):
    """Pass if a tool by `tool_name` was invoked and (if
    `arg_predicates` is set) the parsed `mcae.primitive.input`
    JSON contains all listed key/value matches."""

    kind: Literal["tool_called_with_args"] = "tool_called_with_args"
    tool_name: str
    arg_predicates: dict[str, Any] = Field(default_factory=dict)


class ClaimGroundedInSpec(_ProbeSpecBase):
    """Pass if every `mcae.claim.emitted` span in the trace has
    `mcae.claim.source_kind == source_kind`. Today every claim is
    `primitive`; the `exploratory` value lights up when the planned
    sql_explore tool ships."""

    kind: Literal["claim_grounded_in"] = "claim_grounded_in"
    source_kind: Literal["primitive", "exploratory"]


class GatePassedSpec(_ProbeSpecBase):
    """Pass if the named gate span (`mcae.gate.<gate_kind>`) carries
    `mcae.gate.verdict='approved'` and (if `version` is set)
    `mcae.gate.version=<version>`. The arg name is `gate_kind`
    rather than `kind` because `kind` is the discriminator field."""

    kind: Literal["gate_passed"] = "gate_passed"
    gate_kind: Literal[
        "placeholder", "structural", "constitution", "narrative_constitution"
    ]
    version: str | None = None


class SpanLatencyP50UnderSpec(_ProbeSpecBase):
    """Pass if the median (p50) duration across all matching spans
    in the trace is under `ms` milliseconds. Uses ClickHouse's
    quantile(0.5) aggregate over OTel `Duration` (nanoseconds)."""

    kind: Literal["span_latency_p50_under"] = "span_latency_p50_under"
    span_name: str
    ms: int = Field(gt=0)


class NoSpanWithStatusSpec(_ProbeSpecBase):
    """Pass if no span by `span_name` carries the named status.
    `error` matches our `error=true` attribute convention from
    primitive_client; `ok` matches the absence of an error mark."""

    kind: Literal["no_span_with_status"] = "no_span_with_status"
    span_name: str
    status: Literal["error", "ok"]


class LlmCallUsedModelSpec(_ProbeSpecBase):
    """Pass if any `chat <model>` span in the trace has
    `gen_ai.request.model == model_name`. Useful for asserting the
    eval ran against the expected primary or policy model."""

    kind: Literal["llm_call_used_model"] = "llm_call_used_model"
    model_name: str


# Discriminated union: pydantic dispatches on `kind` at parse time,
# so a YAML case with `kind: claim_grounded_in` and a missing
# `source_kind` fails immediately with a precise error pointing to
# `ClaimGroundedInSpec.source_kind`, not at probe-run time.
ProbeSpec = Annotated[
    Union[
        HasMatchingSpanSpec,
        ToolCalledWithArgsSpec,
        ClaimGroundedInSpec,
        GatePassedSpec,
        SpanLatencyP50UnderSpec,
        NoSpanWithStatusSpec,
        LlmCallUsedModelSpec,
    ],
    Field(discriminator="kind"),
]


# Sanity check: every ProbeKind has a spec class in the union, and
# every union member has a kind in ProbeKind. Raises at import time
# if drift sneaks in.
def _assert_kind_union_exhaustive() -> None:
    union_kinds: set[str] = set()
    for member in get_args(get_args(ProbeSpec)[0]):  # unwrap Annotated, then Union
        kind_field = member.model_fields["kind"]
        union_kinds.add(get_args(kind_field.annotation)[0])
    declared_kinds = set(get_args(ProbeKind))
    if union_kinds != declared_kinds:
        missing = declared_kinds - union_kinds
        extra = union_kinds - declared_kinds
        raise RuntimeError(
            f"ProbeKind / ProbeSpec union drift: missing={missing}, extra={extra}"
        )


_assert_kind_union_exhaustive()


# ---------------------------------------------------------------------------
# Eval case
# ---------------------------------------------------------------------------


class EvalCase(BaseModel):
    """One agent input plus the probes that should pass against the
    OTel trace produced by running it.

    `inputs` is shaped like a production `AgentRequest` (the proto
    canonical-JSON the runner POSTs to `/agent/ask`). The runner
    treats it as opaque; the agent's own validation rejects
    malformed inputs at the API boundary, which is the right place
    for that check.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(
        description=(
            "Stable across renames. Used as the primary key in "
            "evals/runs/<run_id>/<case_id>/ paths and ProbeResult."
        ),
    )
    suite: str = Field(
        description=(
            "Suite name, typically the YAML file's stem with a "
            "qualifier, e.g. 'wallet_profile.smoke'."
        ),
    )
    inputs: dict[str, Any] = Field(
        description="AgentRequest-shaped JSON object POSTed to /agent/ask.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
    probes: list[ProbeSpec]

    @field_validator("case_id", "suite")
    @classmethod
    def _str_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be non-empty")
        return v

    @field_validator("probes")
    @classmethod
    def _probes_non_empty_unique(cls, v: list[ProbeSpec]) -> list[ProbeSpec]:
        if not v:
            raise ValueError(
                "at least one probe required; a case with no probes "
                "asserts nothing about the trace and silently passes"
            )
        ids = [p.probe_id for p in v]
        if len(ids) != len(set(ids)):
            raise ValueError("probe_id values must be unique within a case")
        return v


# ---------------------------------------------------------------------------
# Probe result
# ---------------------------------------------------------------------------


class ProbeResult(BaseModel):
    """One probe's outcome against one trace, persisted as JSON under
    `evals/runs/<run_id>/<case_id>/<probe_id>.json`.

    `observed` is the side-channel for whatever the probe wants the
    eyes-on reviewer to see: matched span ids, latency percentile,
    expected-vs-actual diff. Schema deliberately loose; if a field
    earns its keep across multiple probes, promote it to a typed
    field in a future minor schema bump.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    case_id: str
    probe_id: str
    trace_id: str = Field(
        description=(
            "OTel TraceId of the agent run this probe evaluated. "
            "Lets reviewers cross-reference results back to Langfuse "
            "or the otel.otel_traces table."
        ),
    )
    passed: bool
    inconclusive: bool = Field(
        default=False,
        description=(
            "True when the probe's pass/fail outcome cannot be "
            "trusted because the trace had a terminal provider "
            "failure that prevented the agent from completing the "
            "code path the probe asserts on. Set by the runner after "
            "probes finish, based on `infra_health.has_terminal_"
            "provider_failure`. The baseline diff skips inconclusive "
            "entries entirely (no comparison against baseline), so "
            "transient OpenRouter / network blips do not register "
            "as regressions. See evals/README.md `Inconclusive "
            "results` for the full disposition."
        ),
    )
    score: float | None = Field(
        default=None,
        description=(
            "Optional graded score in [0, 1]. Pure pass/fail probes "
            "leave this None; LLM-as-judge or fuzzy probes set it."
        ),
    )
    observed: dict[str, Any] = Field(default_factory=dict)
    error: str | None = Field(
        default=None,
        description=(
            "Set when the probe could not run (e.g. trace not in CH "
            "yet, transient query failure). Distinguishes 'predicate "
            "is false' from 'predicate could not be evaluated'."
        ),
    )
    started_at: datetime
    finished_at: datetime

    @field_validator("score")
    @classmethod
    def _score_in_unit_interval(cls, v: float | None) -> float | None:
        if v is None:
            return v
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"score must be in [0, 1], got {v}")
        return v


# ---------------------------------------------------------------------------
# Run metadata
# ---------------------------------------------------------------------------


class RunMetadata(BaseModel):
    """One `just eval` invocation's summary, persisted as
    `evals/runs/<run_id>/run.json`.

    `framework_adapter` is recorded so a regression diff that spans
    an adapter swap (only `framework_free` exists today; see ADR 14
    addendum) can flag pass/fail changes that might be
    adapter-induced rather than real agent regressions.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    git_sha: str
    agent_version: str
    framework_adapter: FrameworkAdapter
    case_count: int = Field(ge=0)
    probe_count: int = Field(ge=0)
    pass_count: int = Field(ge=0)
    inconclusive_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Probes whose outcome was suppressed by an infra-health "
            "check (terminal provider failure on the trace). Excluded "
            "from pass_count and from the baseline diff."
        ),
    )

    @model_validator(mode="after")
    def _pass_count_within_probe_count(self) -> "RunMetadata":
        if self.pass_count + self.inconclusive_count > self.probe_count:
            raise ValueError(
                f"pass_count ({self.pass_count}) + inconclusive_count "
                f"({self.inconclusive_count}) > probe_count "
                f"({self.probe_count}); accounting bug upstream"
            )
        return self
