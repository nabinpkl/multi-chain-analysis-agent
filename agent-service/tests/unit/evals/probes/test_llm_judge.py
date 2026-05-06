"""Tests for the llm_judge probe.

Three classes of behavior to cover:

1. Spec-level validators (forbidden families, target_attrs unique).
2. The probe's CH-read path (multi-attr argMaxIf query shape, empty-
   trace fail-fast).
3. The judge-call path with a stubbed Agent (happy path scoring,
   pass_threshold semantics, retry-exhausted error path).

The Agent stub avoids real network calls while exercising the
real pydantic-ai surface the probe depends on.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agent_service.evals.probes import llm_judge
from agent_service.evals.probes.llm_judge import JudgeVerdict
from agent_service.evals.schema import LlmJudgeSpec, ProbeResult
from tests.unit.evals.conftest import FakeChClient


# ---------------------------------------------------------------------------
# Spec validators
# ---------------------------------------------------------------------------


def test_spec_rejects_openai_family() -> None:
    """openai/ is the constitution gate's family. Reusing it as the
    judge causes preference leakage."""
    with pytest.raises(ValidationError, match="forbidden family"):
        LlmJudgeSpec(
            probe_id="p",
            rubric="r",
            target_attrs=["x"],
            model="openai/gpt-4-turbo",
        )


def test_spec_rejects_nvidia_family() -> None:
    """nvidia/ is the primary agent's family."""
    with pytest.raises(ValidationError, match="forbidden family"):
        LlmJudgeSpec(
            probe_id="p",
            rubric="r",
            target_attrs=["x"],
            model="nvidia/nemotron-3-super-120b-a12b:free",
        )


def test_spec_accepts_third_family() -> None:
    spec = LlmJudgeSpec(
        probe_id="p",
        rubric="r",
        target_attrs=["mcae.narrative.text"],
        model="openrouter/owl-alpha",
    )
    assert spec.model == "openrouter/owl-alpha"


def test_spec_rejects_duplicate_target_attrs() -> None:
    with pytest.raises(ValidationError, match="unique"):
        LlmJudgeSpec(
            probe_id="p",
            rubric="r",
            target_attrs=["a", "a"],
            model="google/gemma:free",
        )


def test_spec_rejects_empty_target_attrs() -> None:
    with pytest.raises(ValidationError):
        LlmJudgeSpec(
            probe_id="p",
            rubric="r",
            target_attrs=[],
            model="google/gemma:free",
        )


def test_pass_threshold_must_be_in_unit_interval() -> None:
    with pytest.raises(ValidationError):
        LlmJudgeSpec(
            probe_id="p",
            rubric="r",
            target_attrs=["x"],
            model="google/gemma:free",
            pass_threshold=1.5,
        )


def test_model_falls_back_to_eval_judge_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `model:` is omitted in the YAML, the validator resolves
    EVAL_JUDGE_MODEL at parse time. Pin the resolved value into
    spec.model so probe code stays as-is at runtime."""
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "deepseek/deepseek-r1-distill:free")
    spec = LlmJudgeSpec(
        probe_id="p",
        rubric="r",
        target_attrs=["x"],
        # model omitted on purpose
    )
    assert spec.model == "deepseek/deepseek-r1-distill:free"


def test_model_unset_and_env_unset_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No per-probe model AND no env default = error at YAML parse
    time, not later at probe-call time. Catches misconfigured
    suites before they consume any LLM calls."""
    monkeypatch.delenv("EVAL_JUDGE_MODEL", raising=False)
    with pytest.raises(ValidationError, match="EVAL_JUDGE_MODEL"):
        LlmJudgeSpec(
            probe_id="p",
            rubric="r",
            target_attrs=["x"],
        )


def test_forbidden_family_check_uses_env_derived_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The forbidden-family list reads AGENT_PRIMARY_MODEL and
    AGENT_POLICY_MODEL at validator time. Swap the env, the
    forbidden list moves with it. This test confirms the env-
    driven derivation: with anthropic/ as the primary, anthropic/
    becomes forbidden; with openai/ NOT in either env var, openai/
    is now allowed."""
    monkeypatch.setenv("AGENT_PRIMARY_MODEL", "anthropic/claude-sonnet:free")
    monkeypatch.setenv("AGENT_POLICY_MODEL", "anthropic/claude-haiku:free")
    # anthropic/ should now be forbidden
    with pytest.raises(ValidationError, match="forbidden family"):
        LlmJudgeSpec(
            probe_id="p",
            rubric="r",
            target_attrs=["x"],
            model="anthropic/claude-opus:free",
        )
    # openai/ is no longer matched by either env var, so it's accepted
    spec = LlmJudgeSpec(
        probe_id="p",
        rubric="r",
        target_attrs=["x"],
        model="openai/gpt-4-turbo",
    )
    assert spec.model == "openai/gpt-4-turbo"


# ---------------------------------------------------------------------------
# Probe behavior
# ---------------------------------------------------------------------------


def _spec(
    *,
    rubric: str = "Score 1.0 if narrative is on-topic, else 0.0.",
    target_attrs: list[str] | None = None,
    pass_threshold: float = 0.5,
) -> LlmJudgeSpec:
    return LlmJudgeSpec(
        probe_id="judge_test",
        rubric=rubric,
        target_attrs=target_attrs or ["mcae.narrative.text"],
        model="openrouter/owl-alpha",
        pass_threshold=pass_threshold,
    )


class _StubAgentResult:
    def __init__(self, output: str) -> None:
        self.output = output


class _StubAgent:
    """Stand-in for pydantic_ai.Agent. The probe uses plain-text
    output (output_type=str) and parses JSON from the response, so
    the stub yields a string. Configurable to either yield a string
    or raise."""

    def __init__(
        self,
        text: str | None = None,
        raise_exc: BaseException | None = None,
    ) -> None:
        self._text = text
        self._raise = raise_exc
        self.run_calls: list[str] = []

    async def run(self, prompt: str) -> _StubAgentResult:
        self.run_calls.append(prompt)
        if self._raise is not None:
            raise self._raise
        assert self._text is not None
        return _StubAgentResult(self._text)


def _verdict_text(score: float, reason: str) -> str:
    """Plain-text shape the judge model is asked to produce in the
    system prompt: bare JSON object, no markdown fence, no commentary."""
    return f'{{"score": {score}, "reason": "{reason}"}}'


@pytest.mark.asyncio
async def test_probe_passes_when_score_meets_threshold() -> None:
    spec = _spec(pass_threshold=0.7)
    ch = FakeChClient(
        respond_with=lambda _sql, _p: [{"v0": "the agent's narrative"}]
    )
    stub = _StubAgent(text=_verdict_text(0.9, "on-topic"))
    with patch("agent_service.evals.probes.llm_judge.Agent", return_value=stub):
        r: ProbeResult = await llm_judge.run(
            spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
        )

    assert r.passed is True
    assert r.score == 0.9
    assert r.observed["judge_reason"] == "on-topic"
    assert r.observed["judge_model"] == "openrouter/owl-alpha"
    assert r.observed["pass_threshold"] == 0.7


@pytest.mark.asyncio
async def test_probe_fails_when_score_under_threshold() -> None:
    spec = _spec(pass_threshold=0.7)
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"v0": "x"}])
    stub = _StubAgent(text=_verdict_text(0.4, "off-topic"))
    with patch("agent_service.evals.probes.llm_judge.Agent", return_value=stub):
        r = await llm_judge.run(
            spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
        )

    assert r.passed is False
    assert r.score == 0.4
    assert r.error is None  # not an error; the judge ran and scored low


@pytest.mark.asyncio
async def test_probe_extracts_json_from_response_with_extra_text() -> None:
    """Many models prepend or append commentary even when asked not
    to. The probe must extract the first JSON object from the text
    rather than requiring the entire response to be JSON."""
    spec = _spec(pass_threshold=0.5)
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"v0": "narrative"}])
    response = (
        "Sure, here's my evaluation:\n\n"
        '{"score": 0.85, "reason": "narrative is on-topic"}\n\n'
        "Hope this helps!"
    )
    stub = _StubAgent(text=response)
    with patch("agent_service.evals.probes.llm_judge.Agent", return_value=stub):
        r = await llm_judge.run(
            spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
        )

    assert r.passed is True
    assert r.score == 0.85


@pytest.mark.asyncio
async def test_probe_handles_json_with_braces_in_string_value() -> None:
    """The judge often writes `${ref:N}` literally in its `reason`
    field when commenting on the agent's citation discipline. A
    non-greedy regex stops at the inner `}` and breaks parse;
    json.JSONDecoder.raw_decode handles it correctly. This test
    pins that behavior so a future regression to regex extraction
    is caught at unit-test time, not at parse-failure-during-
    live-eval time."""
    spec = _spec(pass_threshold=0.5)
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"v0": "narrative"}])
    response = (
        '{"score": 1.0, "reason": "The narrative cleanly cites '
        'audit values via ${ref:0} and ${ref:1} placeholders."}'
    )
    stub = _StubAgent(text=response)
    with patch("agent_service.evals.probes.llm_judge.Agent", return_value=stub):
        r = await llm_judge.run(
            spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
        )

    assert r.passed is True
    assert r.score == 1.0
    assert "${ref:0}" in r.observed["judge_reason"]


@pytest.mark.asyncio
async def test_probe_returns_parse_error_when_response_has_no_json() -> None:
    """Judge model produces text without JSON. Probe surfaces this
    as passed=False with a parse_failed outcome rather than crashing."""
    spec = _spec()
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"v0": "narrative"}])
    stub = _StubAgent(text="I think this looks good but I'm not sure.")
    with patch("agent_service.evals.probes.llm_judge.Agent", return_value=stub):
        r = await llm_judge.run(
            spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
        )

    assert r.passed is False
    assert r.error is not None
    assert "parse failed" in r.error
    assert r.observed["judge_call_outcome"] == "parse_failed"
    assert "raw_response_first_500" in r.observed


@pytest.mark.asyncio
async def test_probe_fails_fast_when_no_attrs_have_values() -> None:
    """Trace doesn't have the spans the case author expected (typo
    in attr name, or case exercises the wrong path). Don't waste a
    judge call."""
    spec = _spec(target_attrs=["mcae.bogus.attr1", "mcae.bogus.attr2"])
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"v0": "", "v1": ""}])
    # Patch Agent so any accidental call raises and fails the test
    with patch(
        "agent_service.evals.probes.llm_judge.Agent",
        side_effect=AssertionError("judge should not have been called"),
    ):
        r = await llm_judge.run(
            spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
        )

    assert r.passed is False
    assert r.error is not None
    assert "no target_attrs had non-empty values" in r.error


@pytest.mark.asyncio
async def test_probe_returns_error_when_judge_call_fails_after_retry() -> None:
    """Layer 1 retry exhausted: the judge model is genuinely
    unreachable. Probe surfaces this as passed=False with the
    error field populated, rather than crashing the whole run."""
    spec = _spec()
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"v0": "narrative"}])

    from pydantic_ai.exceptions import UnexpectedModelBehavior

    # Stub.run always raises the retryable exception, so
    # with_provider_retry retries once then re-raises.
    stub = _StubAgent(raise_exc=UnexpectedModelBehavior("provider down"))
    with patch("agent_service.evals.probes.llm_judge.Agent", return_value=stub):
        r = await llm_judge.run(
            spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
        )

    assert r.passed is False
    assert r.error is not None
    assert "judge call failed" in r.error
    assert "UnexpectedModelBehavior" in r.error
    assert r.observed["judge_call_outcome"] == "exception_after_retry"
    # Confirms with_provider_retry ran the stub twice (once + retry).
    assert len(stub.run_calls) == 2


@pytest.mark.asyncio
async def test_probe_query_uses_argmaxif_with_one_param_per_attr() -> None:
    """Probe's CH query must bind each attr name as its own param
    (server-side parameterization). Test inspects the SQL shape so
    a regression to f-string interpolation is caught at unit-test
    time, not at SQL-injection-CVE time."""
    spec = _spec(target_attrs=["mcae.narrative.text", "mcae.gate.constitution.verdict"])
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"v0": "n", "v1": "approved"}])
    stub = _StubAgent(text=_verdict_text(1.0, "ok"))
    with patch("agent_service.evals.probes.llm_judge.Agent", return_value=stub):
        await llm_judge.run(
            spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
        )

    assert len(ch.calls) == 1
    sql, params = ch.calls[0]
    # No f-string interpolation: attr names appear ONLY in params,
    # not embedded in the SQL string.
    assert "mcae.narrative.text" not in sql
    assert "mcae.gate.constitution.verdict" not in sql
    assert params["a0"] == "mcae.narrative.text"
    assert params["a1"] == "mcae.gate.constitution.verdict"
    assert params["tid"] == "trace1"
