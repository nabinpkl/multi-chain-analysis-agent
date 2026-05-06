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

import os
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
    "llm_judge",
    "turn_attribute_equals",
    "slowest_call_under_ms",
    "no_matching_span",
]


# Env vars that drive the agent's stage models. The eval-judge
# forbidden-family list is derived from these at validation time
# so production and the validator share one source of truth: swap
# AGENT_PRIMARY_MODEL or AGENT_POLICY_MODEL in .env, the validator
# picks up the new family on the next process start. No manual
# sync between llm.py and schema.py.
_AGENT_STAGE_MODEL_ENV_VARS: tuple[str, ...] = (
    "AGENT_PRIMARY_MODEL",
    "AGENT_POLICY_MODEL",
)


def _judge_forbidden_families() -> tuple[str, ...]:
    """Family prefixes that the eval-judge MUST NOT use, derived
    from the env vars naming the agent's stage models.

    Reusing the same family for the eval judge causes preference
    leakage (ICLR 2026: same/related model families used as
    generator + judge produce systematic 'judge agrees with
    itself' bias). The validator below uses this list to reject
    any judge model whose family-prefix matches.

    Reads env on every call rather than caching at import time so
    a process started before .env loaded (e.g. a stray subprocess)
    still picks up the right list once env is available. The cost
    is one os.environ lookup per validator invocation, which is
    negligible compared to anything else the validator does.
    """
    families: set[str] = set()
    for var in _AGENT_STAGE_MODEL_ENV_VARS:
        model_id = os.environ.get(var, "")
        if "/" in model_id:
            families.add(model_id.split("/", 1)[0] + "/")
    return tuple(sorted(families))

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


class LlmJudgeSpec(_ProbeSpecBase):
    """LLM-as-judge probe. Reads N span attributes from the trace,
    sends them to a judge model along with the rubric, parses the
    judge's structured response into a score and reason, passes if
    the score meets `pass_threshold`.

    Two design rules baked into the spec:

    1. **Forbidden judge model families, env-derived.** `model`
       must NOT begin with any family-prefix returned by
       `_judge_forbidden_families()`, which reads the same env
       vars (AGENT_PRIMARY_MODEL, AGENT_POLICY_MODEL) that drive
       the agent's stage models in `agent_service/llm.py`. Using
       the same family as any stage of the agent under test
       causes preference leakage (judge biases toward agreeing
       with its own family). Swap a stage model in .env, the
       forbidden list updates automatically; no manual sync.

    2. **Multiple span attrs in one probe.** `target_attrs` is a
       list, not a single attr. Outcome-mode cases pass one entry
       (e.g. `[mcae.narrative.text]`); trajectory-mode cases pass
       several (narrative + gate verdict + claim headline) so the
       judge can audit the agent's full pipeline including its own
       internal judges. The rubric references attribute names
       directly so the case author controls what the judge weighs.

    3. **`model` is optional; falls back to EVAL_JUDGE_MODEL.**
       Most cases inherit the suite-level default from .env so
       there's no per-probe repetition. Set `model:` explicitly
       only when the case needs to override (e.g. A/B comparing
       two judges in one suite). The validator resolves the env
       var at parse time and stores the resolved string, so
       `spec.model` is always concrete at runtime.

    Use sparingly. Deterministic probes (claim_grounded_in,
    structural gate_passed) are strictly more reliable for what
    they assert; this probe fills the qualitative gap they cannot
    reach (tone, off-topic-ness, did-the-answer-match-the-question,
    is-the-explanation-coherent-given-claims, did-the-gate-decide-
    correctly)."""

    kind: Literal["llm_judge"] = "llm_judge"
    rubric: str = Field(
        description=(
            "Free-form English describing what the judge should "
            "score. Reference span attributes by name. Make the "
            "scoring rule explicit (e.g. 'score 1.0 if X else 0.0' "
            "for binary, or 'score 0.0-1.0 based on Y' for graded)."
        ),
    )
    target_attrs: list[str] = Field(
        min_length=1,
        description=(
            "Span attribute names the judge will see, e.g. "
            "['mcae.narrative.text'] for outcome-mode or "
            "['mcae.narrative.text', 'mcae.gate.constitution.verdict'] "
            "for trajectory-mode."
        ),
    )
    model: str | None = Field(
        default=None,
        # validate_default=True makes the resolve-and-check validator
        # below run even when the YAML omits `model:` and pydantic
        # uses the default. Without it, default-None fields skip
        # validators in pydantic v2; the env-fallback path would
        # never fire.
        validate_default=True,
        description=(
            "OpenRouter model id for the judge call. Defaults to the "
            "EVAL_JUDGE_MODEL env var when omitted (the common case). "
            "Set explicitly only to override the suite default for "
            "this specific probe. MUST NOT use a family that any "
            "stage of the agent under test uses; the validator "
            "derives the forbidden list from AGENT_PRIMARY_MODEL "
            "and AGENT_POLICY_MODEL at parse time and rejects matches."
        ),
    )
    pass_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description=(
            "passed=True iff the judge's score >= this. Binary "
            "rubrics tend to use 0.5 (any positive score passes); "
            "graded rubrics tend to use 0.6-0.8 depending on how "
            "strict the case author wants to be."
        ),
    )

    @field_validator("model", mode="after")
    @classmethod
    def _resolve_and_check_model(cls, v: str | None) -> str:
        """Resolve `model` to a concrete id (env-default if None)
        and reject if its family-prefix matches any stage of the
        agent under test (preference leakage prevention).

        Returning a string from a `str | None` field is intentional
        and pydantic-allowed: after this validator the field's
        runtime value is always concrete, even though the YAML
        type permits omission. This keeps probe code simple
        (`spec.model` is always a usable id).
        """
        if v is None:
            v = os.environ.get("EVAL_JUDGE_MODEL", "")
            if not v:
                raise ValueError(
                    "llm_judge probe has no `model` set and "
                    "EVAL_JUDGE_MODEL env var is unset; either set "
                    "the env var (the common case) or pass `model:` "
                    "explicitly in the YAML."
                )
        for prefix in _judge_forbidden_families():
            if v.startswith(prefix):
                raise ValueError(
                    f"judge model {v!r} is in forbidden family "
                    f"{prefix!r}: the agent under test uses this "
                    f"family for one of its stages (per "
                    f"AGENT_PRIMARY_MODEL / AGENT_POLICY_MODEL). "
                    f"Reusing it for the eval judge causes "
                    f"preference leakage; pick a different family."
                )
        return v

    @field_validator("target_attrs")
    @classmethod
    def _target_attrs_unique(cls, v: list[str]) -> list[str]:
        if len(v) != len(set(v)):
            raise ValueError(
                "target_attrs must be unique; duplicates would send "
                "the same value twice to the judge under different "
                "prompt slots, which is wasted tokens."
            )
        return v


class TurnAttributeEqualsSpec(_ProbeSpecBase):
    """Pass if the `mcae.turn` root span's named attribute equals
    the expected value.

    The `mcae.turn` span carries per-turn counts (tool_calls,
    claims_emitted, claims_approved, narrative_chars, run.type,
    user_question) that are useful invariants without being
    implementation-detail-tight. Examples:

      - turn_attribute_equals(attr=mcae.turn.tool_calls, expected="0")
        for refusal cases that should not call any tool
      - turn_attribute_equals(attr=mcae.turn.claims_emitted, expected="1")
        when a case must emit exactly one claim
      - turn_attribute_equals(attr=mcae.run.type, expected="eval")
        equivalent to has_matching_span+attrs but reads more naturally

    `expected` is a string because OTel SpanAttributes are stored
    as `Map(LowCardinality(String), String)` in ClickHouse: integer
    counts come back as their string representation (e.g. "0", "3").
    Case authors write the string form they expect to see in CH.
    """

    kind: Literal["turn_attribute_equals"] = "turn_attribute_equals"
    attr: str = Field(
        description=(
            "Span attribute key on `mcae.turn`, e.g. "
            "`mcae.turn.tool_calls`, `mcae.turn.claims_emitted`, "
            "`mcae.run.type`."
        ),
    )
    expected: str = Field(
        description=(
            "Expected value as it appears in CH "
            "`SpanAttributes[attr]`. All values are strings on the "
            "wire (Map(LowCardinality(String), String)), so write "
            "integers as their string form: '0', '1', '3'."
        ),
    )


class NoMatchingSpanSpec(_ProbeSpecBase):
    """Pass if NO span by `span_name` exists in the trace (and, if
    `attrs` is set, no span with both the name AND all listed
    attribute matches). Direct mirror of `has_matching_span`,
    inverted assertion.

    Lights up the negative path that switches-off cases need: e.g.
    `stayInRole=false` should mean no `mcae.gate.constitution` span
    is emitted on the turn. `has_matching_span` cannot express that;
    `no_span_with_status` requires a status filter and so doesn't
    cover plain absence. Without this probe, switches-off cases
    can't prove the switch actually does something.
    """

    kind: Literal["no_matching_span"] = "no_matching_span"
    span_name: str
    attrs: dict[str, str] = Field(default_factory=dict)


class SlowestCallUnderMsSpec(_ProbeSpecBase):
    """Pass if the slowest LLM call or tool call in the trace is under
    `ms` milliseconds. The probe surfaces the offender's identity in
    `observed` so a failure tells you *which* model or tool stalled,
    not just that something stalled.

    Picks one of two span shapes via `call_kind`:

    - `llm`: pydantic_ai-emitted `chat <model>` spans. The
      `gen_ai.request.model` attribute identifies the offending
      model when the probe fails.
    - `tool`: pydantic_ai-emitted `running tool` spans. The
      `gen_ai.tool.name` attribute identifies the offending tool.

    The diagnostic angle is the load-bearing piece. Existing
    `span_latency_p50_under` answers "is the median fine"; this
    probe answers "is the worst one fine, and which one was it".
    Useful gate against free-tier provider stalls (one slow LLM
    hop is what the per-attempt timeout in `with_provider_retry`
    is meant to catch; this probe pins the threshold so a silent
    regression doesn't slip past the SSE-stream cap).

    Vacuously-passes pitfall: a trace with zero matching spans
    fails with `error='no matching <kind> spans found'`. A turn
    that legitimately calls no tools (refusal case) should not
    use `call_kind=tool`; use `turn_attribute_equals` against
    `mcae.turn.tool_calls` instead.
    """

    kind: Literal["slowest_call_under_ms"] = "slowest_call_under_ms"
    call_kind: Literal["llm", "tool"] = Field(
        description=(
            "Which span family to scan. `llm` matches `chat %` spans "
            "via SpanName LIKE pattern; `tool` matches `running tool%` "
            "spans. The two families have different attribute keys "
            "for the offender's identity (gen_ai.request.model vs "
            "gen_ai.tool.name), so the probe handles them separately."
        ),
    )
    ms: int = Field(
        gt=0,
        description=(
            "Threshold the slowest matching span must come in under. "
            "For LLM calls on free-tier OpenRouter, ~60000 (60s) is "
            "the practical ceiling before the per-attempt timeout in "
            "`with_provider_retry` would have aborted the call. For "
            "tool calls, ~10000 (10s) is generous; primitive HTTP "
            "round-trips to the Rust service should complete in tens "
            "of ms when warm."
        ),
    )


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
        LlmJudgeSpec,
        TurnAttributeEqualsSpec,
        SlowestCallUnderMsSpec,
        NoMatchingSpanSpec,
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
