"""Canonical eval types. Layer 1 of the Ship 2 stack (ADR 14).

These types are the contract every other layer depends on:

- Layer 2 probes return `ProbeResult`.
- Layer 3 runner loads `EvalCase` from YAML, persists `ProbeResult`,
  emits `RunMetadata`.
- Layer 4 adapter translates `EvalCase`/`ProbeSpec` into pydantic_evals
  Case/Evaluator and back.

The four invariants this layer protects:

1. A case is data, not code (YAML-loadable, stable IDs).
2. A probe is a predicate over an OTel trace (kind + args).
3. A probe result is a structured artifact (JSON-persistable).
4. The agent under test is invoked exactly the way production
   invokes it (`inputs` is shaped like the production AgentRequest).

Rules for evolving this file:

- Adding a probe kind: extend `ProbeKind` Literal, add a probe module
  in `probes/`, register in dispatch. No schema migration.
- Adding a probe-kind-specific arg: put it in `ProbeSpec.args` (the
  `dict[str, Any]` is intentional). Do NOT add a sibling field to
  `ProbeSpec` for one probe kind's needs.
- Adding a result field: append to `ProbeResult` with a default. Old
  results stay readable; new probes can populate.
- Renaming any field: requires migrating committed YAML cases AND
  baseline JSON files. Avoid.

This module imports nothing from `agent_service` and nothing from
any eval framework. That is load-bearing per ADR 14 (Layer 1 is a
leaf; framework swap touches Layer 4 only).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

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

# Framework adapters live under `adapters/<value>_adapter.py`. The
# active adapter is recorded on every run so cross-run comparisons
# can detect adapter changes that might have shifted what passes.
FrameworkAdapter = Literal[
    "pydantic_evals",
    "framework_free",
    "inspect_ai",
]


# ---------------------------------------------------------------------------
# Probe spec
# ---------------------------------------------------------------------------


class ProbeSpec(BaseModel):
    """A single predicate the runner evaluates against one trace.

    `kind` selects which probe module runs. `args` is the
    kind-specific parameter bag; each probe module validates its own
    args at call time so this layer stays kind-agnostic.
    """

    model_config = ConfigDict(extra="forbid")

    probe_id: str = Field(
        description=(
            "Stable per (case, probe). Used as the primary key in "
            "ProbeResult so two probes of the same kind on one case "
            "are distinguishable."
        ),
    )
    kind: ProbeKind
    args: dict[str, Any] = Field(default_factory=dict)

    @field_validator("probe_id")
    @classmethod
    def _probe_id_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("probe_id must be non-empty")
        return v


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
    def _probes_non_empty(cls, v: list[ProbeSpec]) -> list[ProbeSpec]:
        if not v:
            raise ValueError(
                "at least one probe required; a case with no probes "
                "asserts nothing about the trace and silently passes"
            )
        # probe_ids must be unique within a case for ProbeResult to key cleanly
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
            "yet, args malformed). Distinguishes 'predicate is false' "
            "from 'predicate could not be evaluated'."
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
    an adapter swap (e.g. pydantic_evals → framework_free) can flag
    pass/fail changes that might be adapter-induced rather than
    real agent regressions.
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

    @model_validator(mode="after")
    def _pass_count_within_probe_count(self) -> "RunMetadata":
        if self.pass_count > self.probe_count:
            raise ValueError(
                f"pass_count ({self.pass_count}) > probe_count "
                f"({self.probe_count}); accounting bug upstream"
            )
        return self
