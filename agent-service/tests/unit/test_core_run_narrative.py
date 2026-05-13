"""Unit coverage for `agent_service.core.run.resolve_narrative_text`.

The helper applies the `channels.narrativeOutputEnabled` switch to
the agent's prose output and stamps the matching cockpit-pattern
instruments on the narrative span. Both runtimes (pydantic-ai loop
driver and codex driver) call this helper at every narrative emit
site so the suppression behavior is uniform across runtimes.

The cockpit pattern: every channel switch ships with a deterministic
OTel observable proving the off-state held. For this switch the
observables are:

  - mcae.narrative.suppressed = True
  - mcae.narrative.pre_suppression_chars = <original length>
  - mcae.narrative.length_chars = 0

When the switch is on, only `mcae.narrative.length_chars = <actual>`
is stamped (no suppression to advertise).

These tests assert the contract directly via the in-memory OTel
exporter shared with `test_codex_driver_units.py`.
"""

from __future__ import annotations

import os

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from agent_service.core.run import resolve_narrative_text


@pytest.fixture
def span_and_exporter():
    """Build an isolated tracer provider + in-memory exporter, open
    a span on it, and yield `(span, exporter)`. The helper writes
    attributes onto the span; the exporter records them once the
    span closes. Reverts on teardown.

    `OTEL_SDK_DISABLED=true` (set in conftest) makes the global
    tracer a no-op, so we build a fresh local provider here to
    capture spans for assertions. Same fixture shape as
    `test_codex_driver_units.span_exporter`.
    """
    prior_disabled = os.environ.pop("OTEL_SDK_DISABLED", None)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test_core_run_narrative")
    span = tracer.start_span("test")
    try:
        yield span, exporter
    finally:
        if not span._end_time:  # type: ignore[attr-defined]
            span.end()
        if prior_disabled is not None:
            os.environ["OTEL_SDK_DISABLED"] = prior_disabled


def _attrs(exporter: InMemorySpanExporter) -> dict:
    """Recover the recorded attribute dict from the (single) span
    captured during the test."""
    finished = exporter.get_finished_spans()
    assert len(finished) == 1, "expected exactly one span"
    return dict(finished[0].attributes or {})


def test_channel_on_returns_text_unchanged(span_and_exporter):
    """Channel on => text flows through, length is stamped, no
    suppression instruments."""
    span, exporter = span_and_exporter
    result = resolve_narrative_text(
        "hello world",
        narrative_output_enabled=True,
        nar_span=span,
    )
    span.end()
    assert result == "hello world"
    attrs = _attrs(exporter)
    assert attrs.get("mcae.narrative.length_chars") == 11
    assert "mcae.narrative.suppressed" not in attrs
    assert "mcae.narrative.pre_suppression_chars" not in attrs


def test_channel_off_returns_empty_and_stamps_cockpit_instruments(
    span_and_exporter,
):
    """Channel off => empty text out, suppressed=true on the span,
    pre_suppression_chars records the original length. This is the
    full cockpit-pattern contract."""
    span, exporter = span_and_exporter
    result = resolve_narrative_text(
        "this is the model's prose that gets dropped",
        narrative_output_enabled=False,
        nar_span=span,
    )
    span.end()
    assert result == ""
    attrs = _attrs(exporter)
    assert attrs.get("mcae.narrative.suppressed") is True
    assert attrs.get("mcae.narrative.length_chars") == 0
    assert attrs.get("mcae.narrative.pre_suppression_chars") == 43


def test_channel_off_with_empty_input_still_stamps_suppressed(
    span_and_exporter,
):
    """Even when the model produced nothing, the suppression flag
    is stamped so probes can distinguish 'cockpit suppressed' from
    'model wrote nothing on its own'. Both paths produce empty SSE
    text but only the suppression path carries the flag."""
    span, exporter = span_and_exporter
    result = resolve_narrative_text(
        "",
        narrative_output_enabled=False,
        nar_span=span,
    )
    span.end()
    assert result == ""
    attrs = _attrs(exporter)
    assert attrs.get("mcae.narrative.suppressed") is True
    assert attrs.get("mcae.narrative.pre_suppression_chars") == 0
