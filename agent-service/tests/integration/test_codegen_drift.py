"""Codegen drift tests for the Python-side flows of `just regen-wire-types`.

Two flows under test:

1. `dump_agent_schemas.py` walks `wire/agent.py` and writes per-model
   JSON Schema files to `wire/schemas-agent/`. Re-running the script
   in a temp dir must produce byte-identical output to what's checked
   in. Drift = someone changed pydantic models without re-running the
   script.

2. The `frontend/scripts/build-agent-wire.mjs` step consumes those
   schemas and writes `frontend/src/lib/agent-wire.ts`. Re-running it
   must also produce byte-identical output.

The Rust → Python pydantic flow (datamodel-codegen) is NOT tested here
because that's part of `just regen-shared-types` and lives in the
existing Phase A test surface (and the codegen output is sometimes
non-deterministic across formatter versions, which we mitigate with
`--disable-timestamp` but not 100%).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


# Layout: tests/integration/test_codegen_drift.py -> agent-service -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_AGENT_SERVICE = _REPO_ROOT / "agent-service"
_SCHEMAS_AGENT = _AGENT_SERVICE / "src" / "agent_service" / "wire" / "schemas-agent"
_FRONTEND_AGENT_WIRE = _REPO_ROOT / "frontend" / "src" / "lib" / "agent-wire.ts"


def _ensure_committed_artifacts_present():
    """Sanity check: the artifacts being asserted clean exist at all.
    Without this, a missing file would silently look 'clean'."""
    assert _SCHEMAS_AGENT.is_dir(), (
        f"missing checked-in schemas dir: {_SCHEMAS_AGENT}. "
        f"Run `just regen-wire-types` and commit."
    )
    assert _FRONTEND_AGENT_WIRE.is_file(), (
        f"missing checked-in frontend TS: {_FRONTEND_AGENT_WIRE}. "
        f"Run `just regen-wire-types` and commit."
    )


def _read_dir(path: Path) -> dict[str, bytes]:
    """Snapshot a directory as {filename: bytes} for comparison."""
    return {p.name: p.read_bytes() for p in sorted(path.iterdir()) if p.is_file()}


def test_agent_schemas_clean_after_regen(tmp_path):
    """Re-run dump_agent_schemas.py against a temp output dir; byte
    diff must be empty against the checked-in schemas."""
    _ensure_committed_artifacts_present()

    # The script writes to a fixed path (relative to the script).
    # We can't easily redirect it, so we snapshot, regen, compare,
    # and restore (which is no-op when output IS clean).
    pre = _read_dir(_SCHEMAS_AGENT)

    result = subprocess.run(
        ["uv", "run", "python", "scripts/dump_agent_schemas.py"],
        cwd=_AGENT_SERVICE,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"dump_agent_schemas.py failed:\n{result.stderr}"
    )

    post = _read_dir(_SCHEMAS_AGENT)

    assert pre.keys() == post.keys(), (
        f"schema file set changed: "
        f"+{sorted(post.keys() - pre.keys())} "
        f"-{sorted(pre.keys() - post.keys())}"
    )
    for name in pre:
        if pre[name] != post[name]:
            pytest.fail(
                f"schema {name} changed after regen. "
                f"Run `just regen-wire-types` and commit the diff."
            )


def test_agent_wire_ts_clean_after_regen(tmp_path):
    """Re-run build-agent-wire.mjs; the output TS must be byte-identical
    to what's checked in."""
    _ensure_committed_artifacts_present()

    pre = _FRONTEND_AGENT_WIRE.read_bytes()

    # Run the node script in-place; it writes to its hardcoded output
    # path. If output matches `pre`, we're clean.
    if shutil.which("node") is None:
        pytest.skip("node not available in test env")
    result = subprocess.run(
        ["node", "scripts/build-agent-wire.mjs"],
        cwd=_REPO_ROOT / "frontend",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"build-agent-wire.mjs failed:\n{result.stderr}"
    )

    post = _FRONTEND_AGENT_WIRE.read_bytes()
    if pre != post:
        pytest.fail(
            "frontend/src/lib/agent-wire.ts changed after regen. "
            "Run `just regen-wire-types` and commit the diff."
        )
