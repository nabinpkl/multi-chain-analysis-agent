"""LLM-as-judge probe: read N span attrs, ask a judge model to score
against a rubric, pass if the score meets `pass_threshold`.

Use sparingly. Deterministic probes are strictly more reliable for
what they assert; this probe fills the qualitative gap they cannot
reach (tone, off-topic-ness, did-the-answer-match-the-question,
is-the-explanation-coherent-given-claims, did-the-gate-decide-
correctly).

The judge call goes through `agent_service.llm_runtime.runtime_call`
which dispatches between two backends based on `AGENT_DEFAULT_RUNTIME`:

- **codex**: spawns / reuses the `mcae-helper` codex subprocess
  authenticated via `~/.codex/auth.json` (ChatGPT subscription, no
  per-call billing). The pydantic schema is wrapped through
  `to_strict_json_schema` and passed as codex's `outputSchema`, so
  the final assistant message is server-enforced JSON. No retry
  wrapper; codex's own internals handle transient hiccups.
- **pydantic_ai**: builds a pydantic-ai `Agent` against the configured
  free-tier provider (Gemini today) and parses the first JSON object
  from the text response. Wrapped in `with_provider_retry` for
  transient-failure handling because free-tier providers occasionally
  return malformed payloads or stall.

Bias prevention: `LlmJudgeSpec.model` is validated at YAML load time
against `_LLM_JUDGE_FORBIDDEN_FAMILIES` so the judge cannot share a
family with any stage of the agent under test (preference leakage,
ICLR 2026). The validator targets pydantic-ai-shaped model ids; on
codex we don't pass `spec.model` because codex picks its model
centrally via `CODEX_HELPER_MODEL`. Operator wires the runtime + the
matching model env; the family guard stays on for the pydantic-ai
path.

Output mode (pydantic-ai path): plain-text completion with manual
JSON extraction, NOT pydantic_ai's `output_type=JudgeVerdict` tool-
calling mode. Many free-tier OpenRouter models don't expose
`tool_choice` (verified empirically 2026-05-06: gemma, baidu/cobuddy,
owl-alpha all 404'd on it). Plain-text completion works on every
text-generation model. `runtime_call` does the parse + validate so
the probe just catches the resulting `ValueError`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from agent_service.evals.ch import ClickHouseClient
from agent_service.evals.schema import LlmJudgeSpec, ProbeResult
from agent_service.llm_runtime import (
    Runtime,
    RuntimeCallParseError,
    resolve_helper_runtime,
    runtime_call,
)

log = structlog.get_logger(__name__)


class JudgeVerdict(BaseModel):
    """Structured shape the judge's response is parsed into. Score in
    [0, 1]; reason is a short operator-facing string for triage.

    `extra="forbid"` so the strict-schema walker in `llm_runtime`
    emits `additionalProperties: false` everywhere the codex path
    requires it. On the pydantic-ai path the same constraint just
    means an unrecognized field would fail validation, which is what
    we want for a judge response."""

    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=500)


_JUDGE_SYSTEM_PROMPT = """You are an automated grader for an AI agent's output.
You read evidence from one of the agent's traces and score it
against a rubric the operator provides.

OUTPUT FORMAT: emit one JSON object and nothing else. The JSON
object MUST have exactly two fields:
  "score": a number between 0.0 and 1.0 inclusive
  "reason": a short string under 500 characters

Example (when the rubric is satisfied):
{"score": 1.0, "reason": "Narrative cleanly addresses the question and stays in role."}

Example (when not):
{"score": 0.0, "reason": "Narrative names the underlying LLM, violating the role constraint."}

Rules:
- Output the JSON object FIRST, before any other text.
- Do NOT wrap the JSON in markdown code fences (```).
- Do NOT add commentary outside the JSON.
- The score is what the rubric asks for; the reason is one or two
  sentences explaining the score so a human auditor can quickly
  decide whether to trust the verdict.
- If the rubric is unclear or the evidence is insufficient, return
  score=0.0 with a reason explaining the ambiguity rather than guessing."""


def _build_user_prompt(rubric: str, attrs: dict[str, str]) -> str:
    """Compose the user-facing message: rubric first, then evidence
    keyed by span attribute name. The judge sees the attr names so
    a trajectory-mode rubric can reference them directly."""
    lines: list[str] = ["RUBRIC:", rubric, "", "EVIDENCE:"]
    for name, value in attrs.items():
        lines.append(f"--- {name} ---")
        lines.append(value if value else "(empty)")
        lines.append("")
    return "\n".join(lines).rstrip()


async def _read_attrs(
    trace_id: str, ch: ClickHouseClient, attrs: list[str]
) -> dict[str, str]:
    """Pull the latest non-empty value for each attribute name across
    all spans in the trace. We don't pin which span carries which
    attr because span ownership of attrs is implementation detail
    of the agent (e.g. mcae.gate.constitution.verdict lives on the
    gate span, mcae.narrative.text lives on the narrative span);
    the case author shouldn't have to know.

    `argMaxIf` picks the value associated with the latest Timestamp
    where the attribute is present and non-empty. If multiple spans
    in one trace set the same attr, the last writer wins. None of
    our agent's emissions overwrite each other within a trace, so
    this is effectively 'find the one span that set this attr'.
    """
    if not attrs:
        return {}
    selects: list[str] = []
    params: dict[str, Any] = {"tid": trace_id}
    for i, attr in enumerate(attrs):
        param_name = f"a{i}"
        selects.append(
            f"argMaxIf(SpanAttributes[{{{param_name}:String}}], Timestamp, "
            f"SpanAttributes[{{{param_name}:String}}] != '') AS v{i}"
        )
        params[param_name] = attr
    sql = (
        f"SELECT {', '.join(selects)} "
        f"FROM otel.otel_traces "
        f"WHERE TraceId = {{tid:String}}"
    )
    rows = await ch.query(sql, **params)
    if not rows:
        return {a: "" for a in attrs}
    row = rows[0]
    return {attr: (row.get(f"v{i}") or "") for i, attr in enumerate(attrs)}


async def run(
    spec: LlmJudgeSpec,
    trace_id: str,
    ch: ClickHouseClient,
    *,
    run_id: str,
    case_id: str,
) -> ProbeResult:
    started = datetime.now(timezone.utc)
    try:
        attrs = await _read_attrs(trace_id, ch, spec.target_attrs)
        if all(not v for v in attrs.values()):
            return ProbeResult(
                run_id=run_id,
                case_id=case_id,
                probe_id=spec.probe_id,
                trace_id=trace_id,
                passed=False,
                observed={
                    "judge_model": spec.model,
                    "target_attrs": spec.target_attrs,
                    "attrs_seen": attrs,
                },
                error=(
                    "no target_attrs had non-empty values on the trace; "
                    "either the trace did not produce these spans (case "
                    "exercises the wrong path) or the attr names are "
                    "wrong (typo against agent_service/spans.py)"
                ),
                started_at=started,
                finished_at=datetime.now(timezone.utc),
            )

        # Runtime selection. The spec's `model` is pydantic-ai-shaped
        # (e.g. "google/gemini-2.5-flash-lite") and the family-guard
        # validator already rejected anything sharing a family prefix
        # with the agent stages. On codex we don't pass it: codex picks
        # its model centrally via `CODEX_HELPER_MODEL`, and the family-
        # guard premise doesn't apply (one provider, one model family,
        # the bias literature on cross-family-vs-same-family is about
        # different inference paths anyway).
        runtime: Runtime = resolve_helper_runtime()
        model_id = spec.model if runtime == "pydantic_ai" else None

        user_prompt = _build_user_prompt(spec.rubric, attrs)

        try:
            verdict, _raw = await runtime_call(
                role="judge",
                system_prompt=_JUDGE_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                output_model=JudgeVerdict,
                runtime=runtime,
                model_id=model_id,
                per_attempt_timeout_s=45.0,
            )
        except RuntimeCallParseError as e:
            # Parse / validation failure (text response did not match
            # JudgeVerdict). Surface as a probe error with the raw
            # diagnostic so the operator can decide.
            log.warning(
                "llm_judge_parse_failed",
                probe_id=spec.probe_id,
                runtime=runtime,
                error=str(e)[:200],
            )
            return ProbeResult(
                run_id=run_id,
                case_id=case_id,
                probe_id=spec.probe_id,
                trace_id=trace_id,
                passed=False,
                observed={
                    "judge_runtime": runtime,
                    "judge_model": spec.model,
                    "target_attrs": spec.target_attrs,
                    "judge_call_outcome": "parse_failed",
                    "raw_response_first_500": e.raw_text[:500],
                },
                error=f"judge response parse failed: {str(e)[:300]}",
                started_at=started,
                finished_at=datetime.now(timezone.utc),
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "llm_judge_call_failed",
                probe_id=spec.probe_id,
                runtime=runtime,
                error_type=type(e).__name__,
                error_message=str(e)[:200],
            )
            return ProbeResult(
                run_id=run_id,
                case_id=case_id,
                probe_id=spec.probe_id,
                trace_id=trace_id,
                passed=False,
                observed={
                    "judge_runtime": runtime,
                    "judge_model": spec.model,
                    "target_attrs": spec.target_attrs,
                    "judge_call_outcome": "exception_after_retry",
                },
                error=f"judge call failed: {type(e).__name__}: {str(e)[:200]}",
                started_at=started,
                finished_at=datetime.now(timezone.utc),
            )

        passed = verdict.score >= spec.pass_threshold
        return ProbeResult(
            run_id=run_id,
            case_id=case_id,
            probe_id=spec.probe_id,
            trace_id=trace_id,
            passed=passed,
            score=verdict.score,
            observed={
                "judge_runtime": runtime,
                "judge_model": spec.model,
                "judge_reason": verdict.reason,
                "pass_threshold": spec.pass_threshold,
                "target_attrs": spec.target_attrs,
            },
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as e:  # noqa: BLE001
        # CH query failed, attr build failed, etc. These are eval-
        # tooling errors not agent-behavior errors; surface as a
        # probe error rather than letting the case loop crash.
        return ProbeResult(
            run_id=run_id,
            case_id=case_id,
            probe_id=spec.probe_id,
            trace_id=trace_id,
            passed=False,
            error=f"probe error: {type(e).__name__}: {str(e)[:200]}",
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )
