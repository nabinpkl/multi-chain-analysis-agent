"""Tests for the llm_judge probe.

Three classes of behavior to cover:

1. Spec-level validators (forbidden families, target_attrs unique).
2. The probe's CH-read path (multi-attr argMaxIf query shape, empty-
   trace fail-fast).
3. The judge-call path with a stubbed `runtime_call` (happy path
   scoring, pass_threshold semantics, parse-failure path, retry-
   exhausted error path).

The probe routes through `agent_service.llm_runtime.runtime_call`,
so tests patch THAT (not pydantic-ai's `Agent` directly) to control
what the judge returns. This stays valid across both runtime
backends (codex, pydantic_ai) because they share the same
`runtime_call` signature.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agent_service.evals.probes import llm_judge
from agent_service.evals.probes.llm_judge import JudgeVerdict
from agent_service.evals.schema import LlmJudgeSpec, ProbeResult
from agent_service.llm_runtime import RuntimeCallParseError
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
    forbidden list moves with it."""
    monkeypatch.setenv("AGENT_PRIMARY_MODEL", "anthropic/claude-sonnet:free")
    monkeypatch.setenv("AGENT_POLICY_MODEL", "anthropic/claude-haiku:free")
    with pytest.raises(ValidationError, match="forbidden family"):
        LlmJudgeSpec(
            probe_id="p",
            rubric="r",
            target_attrs=["x"],
            model="anthropic/claude-opus:free",
        )
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


def _stub_runtime_call(
    *,
    verdict: JudgeVerdict | None = None,
    raise_exc: BaseException | None = None,
):
    """Build an async stub for `runtime_call`. Either returns a
    (verdict, raw_text) tuple or raises the supplied exception."""
    calls: list[dict] = []

    async def _stub(**kwargs) -> tuple[JudgeVerdict, str]:
        calls.append(kwargs)
        if raise_exc is not None:
            raise raise_exc
        assert verdict is not None
        return verdict, verdict.model_dump_json()

    _stub.calls = calls  # type: ignore[attr-defined]
    return _stub


@pytest.mark.asyncio
async def test_probe_passes_when_score_meets_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", "pydantic_ai")
    spec = _spec(pass_threshold=0.7)
    ch = FakeChClient(
        respond_with=lambda _sql, _p: [{"v0": "the agent's narrative"}]
    )
    stub = _stub_runtime_call(verdict=JudgeVerdict(score=0.9, reason="on-topic"))
    with patch("agent_service.evals.probes.llm_judge.runtime_call", stub):
        r: ProbeResult = await llm_judge.run(
            spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
        )

    assert r.passed is True
    assert r.score == 0.9
    assert r.observed["judge_reason"] == "on-topic"
    assert r.observed["judge_model"] == "openrouter/owl-alpha"
    assert r.observed["judge_runtime"] == "pydantic_ai"
    assert r.observed["pass_threshold"] == 0.7
    # On pydantic_ai runtime, spec.model is forwarded as model_id.
    assert stub.calls[0]["model_id"] == "openrouter/owl-alpha"
    assert stub.calls[0]["runtime"] == "pydantic_ai"


@pytest.mark.asyncio
async def test_probe_fails_when_score_under_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", "pydantic_ai")
    spec = _spec(pass_threshold=0.7)
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"v0": "x"}])
    stub = _stub_runtime_call(verdict=JudgeVerdict(score=0.4, reason="off-topic"))
    with patch("agent_service.evals.probes.llm_judge.runtime_call", stub):
        r = await llm_judge.run(
            spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
        )

    assert r.passed is False
    assert r.score == 0.4
    assert r.error is None  # not an error; the judge ran and scored low


@pytest.mark.asyncio
async def test_probe_returns_parse_error_with_raw_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Judge model produces text without JSON. The probe surfaces it
    as passed=False with a parse_failed outcome and stashes the raw
    text on `observed.raw_response_first_500` for operator triage."""
    monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", "pydantic_ai")
    spec = _spec()
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"v0": "narrative"}])
    raw = "I think this looks good but I'm not sure."
    stub = _stub_runtime_call(
        raise_exc=RuntimeCallParseError("no JSON object in response", raw)
    )
    with patch("agent_service.evals.probes.llm_judge.runtime_call", stub):
        r = await llm_judge.run(
            spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
        )

    assert r.passed is False
    assert r.error is not None
    assert "parse failed" in r.error
    assert r.observed["judge_call_outcome"] == "parse_failed"
    assert r.observed["raw_response_first_500"] == raw


@pytest.mark.asyncio
async def test_probe_fails_fast_when_no_attrs_have_values() -> None:
    """Trace doesn't have the spans the case author expected (typo
    in attr name, or case exercises the wrong path). Don't waste a
    judge call."""
    spec = _spec(target_attrs=["mcae.bogus.attr1", "mcae.bogus.attr2"])
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"v0": "", "v1": ""}])
    stub = _stub_runtime_call(
        raise_exc=AssertionError("judge should not have been called")
    )
    with patch("agent_service.evals.probes.llm_judge.runtime_call", stub):
        r = await llm_judge.run(
            spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
        )

    assert r.passed is False
    assert r.error is not None
    assert "no target_attrs had non-empty values" in r.error


@pytest.mark.asyncio
async def test_probe_returns_error_when_judge_call_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runtime_call exhausted its retries (pydantic_ai branch) or
    codex raised a terminal error. Probe surfaces this as
    passed=False with the error field populated, rather than
    crashing the whole run."""
    monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", "pydantic_ai")
    spec = _spec()
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"v0": "narrative"}])

    from pydantic_ai.exceptions import UnexpectedModelBehavior

    stub = _stub_runtime_call(raise_exc=UnexpectedModelBehavior("provider down"))
    with patch("agent_service.evals.probes.llm_judge.runtime_call", stub):
        r = await llm_judge.run(
            spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
        )

    assert r.passed is False
    assert r.error is not None
    assert "judge call failed" in r.error
    assert "UnexpectedModelBehavior" in r.error
    assert r.observed["judge_call_outcome"] == "exception_after_retry"


@pytest.mark.asyncio
async def test_codex_runtime_does_not_forward_spec_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the codex runtime, `spec.model` is pydantic-ai-shaped and
    codex picks its model via `CODEX_HELPER_MODEL` env. The probe
    must pass `model_id=None` on codex so codex's central config
    is the source of truth."""
    monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", "codex")
    spec = _spec()
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"v0": "n"}])
    stub = _stub_runtime_call(verdict=JudgeVerdict(score=1.0, reason="ok"))
    with patch("agent_service.evals.probes.llm_judge.runtime_call", stub):
        await llm_judge.run(
            spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
        )

    assert stub.calls[0]["runtime"] == "codex"
    assert stub.calls[0]["model_id"] is None


@pytest.mark.asyncio
async def test_probe_query_uses_argmaxif_with_one_param_per_attr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe's CH query must bind each attr name as its own param
    (server-side parameterization). Test inspects the SQL shape so
    a regression to f-string interpolation is caught at unit-test
    time, not at SQL-injection-CVE time."""
    monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", "pydantic_ai")
    spec = _spec(target_attrs=["mcae.narrative.text", "mcae.gate.constitution.verdict"])
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"v0": "n", "v1": "approved"}])
    stub = _stub_runtime_call(verdict=JudgeVerdict(score=1.0, reason="ok"))
    with patch("agent_service.evals.probes.llm_judge.runtime_call", stub):
        await llm_judge.run(
            spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
        )

    assert len(ch.calls) == 1
    sql, params = ch.calls[0]
    assert "mcae.narrative.text" not in sql
    assert "mcae.gate.constitution.verdict" not in sql
    assert params["a0"] == "mcae.narrative.text"
    assert params["a1"] == "mcae.gate.constitution.verdict"
    assert params["tid"] == "trace1"
