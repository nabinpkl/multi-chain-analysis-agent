"""OpenTelemetry bootstrap for the agent service.

Ship 1 of the agent-observability foundation (ADR 13). Single source
of truth for agent telemetry is OTel spans, fanned out by an
otel-collector sidecar to (a) our existing ClickHouse instance under
the `otel` database for SQL + cross-store joins with `multichain`,
and (b) Langfuse for the human debugging UI.

The two design rules from ADR 13 inform what lives here:

1. Telemetry layer doesn't know about consumers. We emit OTel spans;
   we don't ask whether anyone is reading them. Consumer routing is
   the otel-collector's problem, configured in
   `infra/otel-collector-config.yaml`.

2. Persistence layer doesn't know about business meaning. We don't
   define schemas here; the ClickHouse exporter auto-creates
   `otel.otel_traces` on first write, and span semantics live in
   span names + attributes the producer chose.

Pydantic AI's `Agent.instrument_all()` gives us OTel GenAI semconv
spans (`agent.run`, `gen_ai.chat`, `execute_tool`) for free. Domain
spans (`gate.*`, `claim.emitted`, `primitive.*`, etc.) are added at
their respective call sites in `loop_driver.py` and
`primitive_client.py` using the `Tracer` returned from `init_otel()`.

Knobs (env vars):
- `OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://otel-collector:4318`)
  HTTP endpoint of the collector. Path `/v1/traces` is appended by the
  exporter. Local dev outside compose can override with
  `http://localhost:4318`.
- `OTEL_SDK_DISABLED=true` short-circuits everything to a no-op
  TracerProvider. Used by the test suite so wiring tests don't try to
  reach a collector that isn't running.
- `OTEL_SERVICE_NAME` is set by `init_otel(service_name)`; honoured by
  the SDK as the Resource attribute.
"""

from __future__ import annotations

import os
from typing import Optional

import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pydantic_ai import Agent
from pydantic_ai.agent import InstrumentationSettings

log = structlog.get_logger(__name__)

# Module-level handle so callers (loop_driver, primitive_client) can do
# `from .otel import tracer` once init_otel() has run. Set by init_otel.
tracer: trace.Tracer = trace.get_tracer("agent-service")


def init_otel(service_name: str = "multichain-agent") -> trace.Tracer:
    """Build a TracerProvider, register OTLP HTTP export to the
    collector, set it global, and call `Agent.instrument_all()`.

    Idempotent: calling twice in the same process is a no-op the
    second time (a TracerProvider is already set globally).
    Returns the tracer for `service_name` either way so callers can
    capture it once at startup.
    """
    global tracer

    if os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        # Pytest path. Leave the default no-op provider in place;
        # spans become silent. Callers using `tracer.start_as_current_span`
        # still work; they just don't export anything.
        log.info("otel_disabled_via_env")
        tracer = trace.get_tracer(service_name)
        return tracer

    # If something already registered a provider in this process, don't
    # stomp on it. This makes init_otel idempotent under uvicorn reload
    # and keeps tests that build their own provider in control.
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        log.info("otel_already_initialized", provider=type(current).__name__)
        tracer = trace.get_tracer(service_name)
        return tracer

    endpoint = os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318"
    )
    # OTLPSpanExporter appends /v1/traces to the base endpoint.
    exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # Pydantic AI: emit GenAI semconv spans for every agent.run +
    # gen_ai.chat + execute_tool. include_content=False keeps tool
    # results (which can be 50KB primitive payloads) out of span
    # attributes; we record digests in our own primitive.* spans.
    # use_aggregated_usage_attribute_names=True consolidates the
    # input/output token counts onto the LLM span as plain
    # gen_ai.usage.input_tokens / output_tokens, which is what our
    # SQL aggregations expect.
    Agent.instrument_all(
        InstrumentationSettings(
            tracer_provider=provider,
            include_content=False,
            use_aggregated_usage_attribute_names=True,
        )
    )

    tracer = trace.get_tracer(service_name)
    log.info("otel_initialized", service_name=service_name, endpoint=endpoint)
    return tracer


def instrument_fastapi(app, excluded_urls: Optional[str] = None) -> None:
    """Auto-instrument the FastAPI app so every HTTP request becomes a
    server span. The agent-stream SSE response then nests its
    Pydantic AI agent.run + custom domain spans underneath, giving
    us a single trace per browser request.

    `excluded_urls` is a comma-separated path list (regex, OTel
    convention). We exclude `/health` so liveness probes don't flood
    the collector.
    """
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls=excluded_urls or "health",
    )
    log.info("fastapi_instrumented", excluded_urls=excluded_urls or "health")
