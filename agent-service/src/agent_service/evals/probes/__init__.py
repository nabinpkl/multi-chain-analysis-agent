"""Probe registry. Maps each `ProbeKind` to its implementing module's
`run` function.

Each probe module under this package exposes one async function:

    async def run(
        spec: <ProbeSpec subclass>,
        trace_id: str,
        ch: ClickHouseClient,
        *,
        run_id: str,
        case_id: str,
    ) -> ProbeResult

The dispatch table below is the only place that knows which kind
maps to which module. The runner (Layer 3) calls
`probes.dispatch(spec.kind)(spec, trace_id, ch, ...)` without
knowing or caring which probe is running.

`_assert_registry_exhaustive()` runs at import time and fails loudly
if the schema gains a probe kind that no module implements. Mirrors
schema.py's `_assert_kind_union_exhaustive`.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, get_args

from agent_service.evals.schema import ProbeKind, ProbeResult
from agent_service.evals.probes import (
    claim_grounded_in,
    gate_passed,
    has_matching_span,
    llm_call_used_model,
    llm_judge,
    no_matching_span,
    no_span_with_status,
    slowest_call_under_ms,
    span_latency_p50_under,
    tool_called_with_args,
    turn_attribute_equals,
)

# `Any` for spec because the dispatched function takes one of seven
# different *Spec subclasses; the discriminator in the schema's
# union ensures the runtime type matches the kind string.
ProbeRunner = Callable[..., Awaitable[ProbeResult]]

_REGISTRY: dict[str, ProbeRunner] = {
    "has_matching_span": has_matching_span.run,
    "tool_called_with_args": tool_called_with_args.run,
    "claim_grounded_in": claim_grounded_in.run,
    "gate_passed": gate_passed.run,
    "span_latency_p50_under": span_latency_p50_under.run,
    "no_span_with_status": no_span_with_status.run,
    "llm_call_used_model": llm_call_used_model.run,
    "llm_judge": llm_judge.run,
    "turn_attribute_equals": turn_attribute_equals.run,
    "slowest_call_under_ms": slowest_call_under_ms.run,
    "no_matching_span": no_matching_span.run,
}


def dispatch(kind: str) -> ProbeRunner:
    """Look up the probe runner for a given kind. Raises `KeyError`
    if the kind is not registered, which cannot happen at runtime
    because the schema's `ProbeKind` Literal is closed and
    `_assert_registry_exhaustive` runs at import."""
    return _REGISTRY[kind]


def _assert_registry_exhaustive() -> None:
    declared = set(get_args(ProbeKind))
    registered = set(_REGISTRY)
    if declared != registered:
        missing = declared - registered
        extra = registered - declared
        raise RuntimeError(
            f"probe registry / ProbeKind drift: missing={missing}, extra={extra}"
        )


_assert_registry_exhaustive()
