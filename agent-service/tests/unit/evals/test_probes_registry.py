"""Sanity check that the probe registry covers every ProbeKind.

The registry's import-time `_assert_registry_exhaustive` raises
RuntimeError if drift exists, so the import alone is the test:
this module loads cleanly only when the registry matches the
schema's closed Literal. The explicit assertion below makes the
intent visible in the test report.
"""

from __future__ import annotations

from typing import get_args

from agent_service.evals import probes
from agent_service.evals.schema import ProbeKind


def test_registry_covers_every_probe_kind() -> None:
    declared = set(get_args(ProbeKind))
    registered = set(probes._REGISTRY)
    assert declared == registered


def test_dispatch_returns_callable_per_kind() -> None:
    for kind in get_args(ProbeKind):
        runner = probes.dispatch(kind)
        assert callable(runner)
