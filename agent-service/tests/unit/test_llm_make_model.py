"""Direct tests for `agent_service.llm.make_model`. Bypasses the autouse
`_no_live_llm` fixture (which monkeypatches `make_model` itself in
`conftest.py`) by importing `_resolve_provider` and exercising the
data-driven dispatch directly. Asserts the right provider config
shows up for each role + override combo without standing up an Agent
or hitting any network.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from agent_service import llm as llm_mod

# These tests exercise the real `make_model`; opt out of the autouse
# `_no_live_llm` stub. No network is touched; we just construct
# Pydantic AI model objects and inspect their provider config.
pytestmark = pytest.mark.real_llm


@pytest.fixture(autouse=True)
def _set_required_env(monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "sk-test-or-dummy")
    monkeypatch.setenv("AGENT_PRIMARY_MODEL", "nvidia/nemotron-test")
    monkeypatch.setenv("AGENT_POLICY_MODEL", "openai/gpt-oss-test")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "openrouter/judge-test")


def _model_provider(model):
    """Pull the underlying `OpenAIProvider` (or any provider with
    `base_url` / `api_key` attrs) off a Pydantic AI Agent model.
    Pydantic AI's `OpenAIChatModel` exposes the provider via
    `_provider`; if that internal name shifts, this test surfaces
    a clear AttributeError pointing at the wrong assumption."""
    return getattr(model, "_provider", None) or model.provider  # type: ignore[attr-defined]


def test_default_path_uses_openrouter():
    model = llm_mod.make_model("primary")
    p = _model_provider(model)
    assert p.base_url.rstrip("/") == "https://openrouter.ai/api/v1"
    # `api_key` isn't a public attr on `OpenAIProvider`; the assertion
    # we get for free is "construction succeeded with the env-driven
    # AGENT_API_KEY," which the fixture set above.
    assert model.model_name == "nvidia/nemotron-test"


def test_empty_override_is_treated_as_default():
    """Empty `RoleOverride` (default proto: provider="" model_id="")
    must fall through to the env-driven OpenRouter path. The wire
    field is always present in proto3; absence is signalled by
    empty strings, not by None."""
    empty = SimpleNamespace(provider="", model_id="")
    model = llm_mod.make_model("primary", override=empty)
    p = _model_provider(model)
    assert p.base_url.rstrip("/") == "https://openrouter.ai/api/v1"


@pytest.mark.parametrize("role", ["primary", "policy", "judge"])
def test_local_override_routes_to_lm_studio(role, monkeypatch):
    """Per-role local override picks up `LOCAL_LLM_BASE_URL` and the
    override's `model_id`. Default LOCAL_LLM_BASE_URL when env is
    unset is `host.docker.internal:1234/v1` (the Docker Desktop
    name; Linux compose's `extra_hosts` provides this on its side)."""
    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    override = SimpleNamespace(provider="local", model_id="qwen2.5-7b-instruct")
    model = llm_mod.make_model(role, override=override)
    p = _model_provider(model)
    assert p.base_url.rstrip("/") == "http://host.docker.internal:1234/v1"
    assert model.model_name == "qwen2.5-7b-instruct"


def test_local_override_respects_custom_base_url(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://192.168.1.50:1234/v1")
    override = SimpleNamespace(provider="local", model_id="qwen-2.5-32b")
    model = llm_mod.make_model("primary", override=override)
    p = _model_provider(model)
    assert p.base_url.rstrip("/") == "http://192.168.1.50:1234/v1"
    assert model.model_name == "qwen-2.5-32b"


def test_local_override_without_model_id_falls_back_to_env_id():
    """Edge case: provider=local but model_id="". The override is
    treated as "use local, but I don't know which model to pick";
    we fall through to the role's env model id. This is degenerate
    user input from the frontend (a half-filled override) and we
    pick the safest interpretation rather than raise."""
    override = SimpleNamespace(provider="local", model_id="")
    model = llm_mod.make_model("primary", override=override)
    p = _model_provider(model)
    assert p.base_url.rstrip("/") == "http://host.docker.internal:1234/v1"
    assert model.model_name == "nvidia/nemotron-test"  # AGENT_PRIMARY_MODEL


def test_unknown_provider_falls_through_to_openrouter():
    """Forward-compat: a future frontend may send a provider name
    this backend doesn't know. Permissive parse: treat as default,
    log nothing, route to OpenRouter."""
    override = SimpleNamespace(provider="anthropic-native", model_id="claude-x")
    model = llm_mod.make_model("primary", override=override)
    p = _model_provider(model)
    assert p.base_url.rstrip("/") == "https://openrouter.ai/api/v1"


def test_explicit_model_id_arg_wins_over_override(monkeypatch):
    """The `model_id=...` kwarg is the eval-judge probe path: the
    case YAML supplies a specific judge id, validated for forbidden-
    family by the spec validator before reaching here. It must
    win over any frontend-supplied override."""
    override = SimpleNamespace(provider="local", model_id="qwen-from-frontend")
    model = llm_mod.make_model("judge", override=override, model_id="judge-from-yaml")
    assert model.model_name == "judge-from-yaml"


def test_missing_required_env_raises():
    """Missing AGENT_PRIMARY_MODEL on the default path should raise
    a clear RuntimeError, not a deeper pydantic-ai error."""
    os.environ.pop("AGENT_PRIMARY_MODEL", None)
    with pytest.raises(RuntimeError, match="AGENT_PRIMARY_MODEL"):
        llm_mod.make_model("primary")
