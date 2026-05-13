"""Unit coverage for `agent_service.main._resolve_default_runtime`.

Runtime selection on `POST /agent/turn` is server-config-driven:
an `AgentRequest` arrives with `runtime=AGENT_RUNTIME_UNSPECIFIED`,
and the server resolves the default from `AGENT_DEFAULT_RUNTIME`
env (matching how `AGENT_PRIMARY_MODEL` / `AGENT_POLICY_MODEL` are
already env-driven). The eval substrate stays runtime-agnostic
this way: operators flip the env and re-run the same suite to
exercise either runtime, instead of duplicating YAMLs.

**Default is codex.** Env unset → codex; pydantic-ai is opt-in via
explicit `AGENT_DEFAULT_RUNTIME=pydantic_ai`. Unrecognized values
fall back to codex with a warning.

Tests cover: default-of-default (env unset), each accepted spelling
of both runtimes, and the unrecognized-value soft-fallback.
"""

from __future__ import annotations

import os

import pytest

from agent_service.main import _resolve_default_runtime
from multichain.wire.agent.v1 import session_pb2 as sess_pb


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test runs with AGENT_DEFAULT_RUNTIME unset by default;
    cases that need a specific value set it via `monkeypatch.setenv`."""
    monkeypatch.delenv("AGENT_DEFAULT_RUNTIME", raising=False)


def test_default_when_env_unset_is_codex():
    """Default of default. Codex is the primary runtime; every
    existing YAML that doesn't pin `inputs.runtime` targets codex.
    Pydantic-ai is opt-in via the env."""
    assert _resolve_default_runtime() == sess_pb.AGENT_RUNTIME_CODEX


def test_explicit_pydantic_ai_short_form(monkeypatch: pytest.MonkeyPatch):
    """The pydantic-ai opt-out path: `AGENT_DEFAULT_RUNTIME=pydantic_ai`
    on the agent-service container reverts every UNSPECIFIED request
    to the legacy pydantic-ai runtime without touching any YAMLs."""
    monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", "pydantic_ai")
    assert _resolve_default_runtime() == sess_pb.AGENT_RUNTIME_PYDANTIC_AI


def test_explicit_pydantic_ai_full_form(monkeypatch: pytest.MonkeyPatch):
    """Full proto-enum name also accepted so operators copying from
    log lines (which print `sess_pb.AgentRuntime.Name(...)`) don't
    get tripped up."""
    monkeypatch.setenv(
        "AGENT_DEFAULT_RUNTIME", "AGENT_RUNTIME_PYDANTIC_AI"
    )
    assert _resolve_default_runtime() == sess_pb.AGENT_RUNTIME_PYDANTIC_AI


def test_explicit_codex_short_form(monkeypatch: pytest.MonkeyPatch):
    """Explicit `codex` value matches the default behavior; covers
    the env-set-but-redundant case so operators can be explicit in
    docker-compose without surprises."""
    monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", "codex")
    assert _resolve_default_runtime() == sess_pb.AGENT_RUNTIME_CODEX


def test_explicit_codex_full_form(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", "AGENT_RUNTIME_CODEX")
    assert _resolve_default_runtime() == sess_pb.AGENT_RUNTIME_CODEX


def test_case_insensitive(monkeypatch: pytest.MonkeyPatch):
    """Operators sometimes capitalize the enum suffix; we lowercase
    on read so `CODEX`, `Codex`, `codex` all work the same way."""
    monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", "CODEX")
    assert _resolve_default_runtime() == sess_pb.AGENT_RUNTIME_CODEX


def test_whitespace_is_trimmed(monkeypatch: pytest.MonkeyPatch):
    """Docker-compose env interpolation occasionally leaves stray
    spaces; one accidental space shouldn't silently flip the whole
    service back to the wrong default."""
    monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", "  pydantic_ai  ")
    assert _resolve_default_runtime() == sess_pb.AGENT_RUNTIME_PYDANTIC_AI


def test_empty_string_is_codex_default(monkeypatch: pytest.MonkeyPatch):
    """Empty env var is treated as unset (defaults to codex).
    Matters because docker-compose's `${VAR:-}` interpolation
    produces an empty string when the host var is missing."""
    monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", "")
    assert _resolve_default_runtime() == sess_pb.AGENT_RUNTIME_CODEX


def test_unrecognized_value_falls_back_to_codex(
    monkeypatch: pytest.MonkeyPatch,
):
    """Typo defense. A bogus value falls back to the codex default
    and emits a warning (visible in agent-service logs as
    `agent_default_runtime_unrecognized`) rather than silently
    flipping to something unintended. Tested without inspecting
    the log output since structlog config varies by environment;
    the contract is "return the codex default."
    """
    monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", "claude-code")
    assert _resolve_default_runtime() == sess_pb.AGENT_RUNTIME_CODEX
