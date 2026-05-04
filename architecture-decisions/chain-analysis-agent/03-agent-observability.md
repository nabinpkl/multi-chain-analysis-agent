# 03: Agent observability foundation (OpenTelemetry + Langfuse)

This document records the decision to lay the agent observability
substrate as OpenTelemetry spans (single source of truth, replacing
the bespoke ledger introduced in Phase II), persisted via fan-out
to two ClickHouse instances and a self-hosted Langfuse stack.

## Status

Accepted, 2026-05-04. Implementation pending in Ship 1 per the plan
at `/Users/nabin/.claude/plans/agent-observability.md`.

## Problem

The Phase II Python rewrite (ADR 02) shipped without per-step
telemetry. The ad-hoc `agent_ledger` table writes only 3 of the 10
event kinds the Rust version had: `session_started`, `turn_diff`,
`turn_completed`. The remaining 7 (`llm_call`, `llm_response`,
`tool_call`, `tool_result`, `policy_verdict`, `prompt`,
`claim_emitted`) were dropped in the port.

Concrete symptom that surfaced the gap: a 2026-05-04 turn took 82
seconds. The ledger row says `tool_calls=1, claims_approved=1`. From
data alone we cannot tell how much of those 82 seconds was the LLM
call, the structural gate, the constitution gate, the primitive
HTTP hop, or the snapshot lease.

Three forks-in-the-road shaped the architecture below.

1. **Build observability or eval first?** Observability. Eval probes
   that fail without per-step traces send debugging back to print
   statements. The trace data is also the eval input, so the
   substrate must be designed once for both consumers.

2. **Restore the bespoke ledger or replace it with OTel spans?** OTel
   spans. Pydantic AI emits OTel GenAI semantic conventions for free
   (LLM calls, tool calls, agent runs). Custom domain spans (gates,
   claims, primitive calls) extend the same SDK. One writer, no
   drift risk between two parallel records, the industry-standard
   format.

3. **Specialized LLM-debugging UI plus fan-out, or unified single-store
   ClickStack?** Specialized + fan-out. Detailed in the rationale
   section below.

## Decision

A three-layer architecture, with single source of truth at the
producer level and two query-optimized projections at the consumer
level.

```
                         CONSUMERS
┌───────────────┬───────────────┬───────────────┬──────────────┐
│ Observability │ Eval          │ Cost tracking │ (future)     │
│ (this ship)   │ (next ship)   │ (deferred)    │ alerts, etc. │
│               │               │               │              │
│ - Langfuse UI │ - probes vs   │ - aggregates  │              │
│   (humans)    │   spans       │   over usage  │              │
│ - Claude+SQL  │ - regression  │   attrs       │              │
│   (targeted)  │   detection   │               │              │
└───────┬───────┴───────┬───────┴───────┬───────┴──────┬───────┘
        └───────────────┴───────────────┴──────────────┘
                              │
                              ▼ all read from same place
        ┌─────────────────────────────────────────────┐
        │              PERSISTENCE                     │
        │                                              │
        │  CH-A (existing data-plane):                 │
        │   - multichain (Rust writes; graph data)     │
        │   - otel       (our spans for SQL+joins)     │
        │                                              │
        │  CH-B (Langfuse-dedicated):                  │
        │   - Langfuse internals (their schema)        │
        │                                              │
        │  + langfuse-postgres (app metadata)          │
        │  + redis (Langfuse cache/queue)              │
        │  + minio (Langfuse blob storage)             │
        └───────────────────┬──────────────────────────┘
                            │
                            ▼ both fed from
                  ┌───────────────────────────┐
                  │     OTel collector         │
                  │   (one ingest, fan-out)    │
                  └───────────────┬────────────┘
                                  │
                                  ▼ standard OTLP
        ┌─────────────────────────┴────────────────────────┐
        │                  TELEMETRY                       │
        │                                                  │
        │  Foundation (Pydantic AI gives us free):         │
        │  - agent.run                                     │
        │  - gen_ai.chat                                   │
        │  - execute_tool                                  │
        │                                                  │
        │  Domain layer (we add):                          │
        │  - gate.{placeholder,structural,constitution}    │
        │  - claim.emitted, narrative.emitted              │
        │  - primitive.{wallet_profile,community_summary}  │
        │  - repeat.detection, turn.diff, snapshot.lease   │
        └──────────────────────────────────────────────────┘
```

### Two design rules

**Rule 1: telemetry layer doesn't know about consumers.** Spans are
emitted with the same structure regardless of who's watching. No
`if eval_mode: emit_extra`. Consumers filter via root-span attributes
(`run.type = "production" | "eval" | "dev"`).

This is what lets eval (Ship 2) plug in without touching
instrumentation.

**Rule 2: persistence layer doesn't know about business meaning.**
The `otel_traces` table is generic: `trace_id, span_id,
parent_span_id, name, attributes (Map), start_ts, end_ts`. Business
meaning lives in span names + attributes the producer chose;
consumers re-derive meaning by querying. Adding a new span kind is
emitting it, not migrating a schema.

### Wire format and conventions

OpenTelemetry GenAI semantic conventions for LLM-related spans
(`gen_ai.system`, `gen_ai.usage.input_tokens`, `agent.tool.name`,
etc). Pydantic AI emits these natively via
`Agent.instrument_all(InstrumentationSettings(use_aggregated_usage_attribute_names=True))`.
Custom domain spans (gates, claims, primitive calls) follow the
same lowercase dot-namespace convention.

### ClickHouse layout

Two ClickHouse instances:

- **CH-A (existing `clickhouse` service):** holds `multichain`
  (Rust-written graph data) + a new `otel` database (our spans via
  the official `clickhouseexporter`). Co-locating these two databases
  in one CH server enables SQL joins between agent behavior traces
  and underlying domain entities.
- **CH-B (new `clickhouse-langfuse` service):** dedicated to Langfuse
  v3.172.1. Stores Langfuse's internal schema. Never queried directly
  by us; Langfuse's UI is its only consumer.

### Self-hosted Langfuse

Pinned to v3.172.1 (released 2026-05-01). Stack: `langfuse-web`,
`langfuse-worker`, `clickhouse-langfuse` (CH-B), `langfuse-postgres`,
`langfuse-redis`, `langfuse-minio`. Langfuse's official
`docker-compose.yml` from their repo is the source of truth; we copy,
swap in our pinned versions, and run as a cohesive stack.

### Wire format per hop (additions to AGENTS.md matrix)

| Hop | Format | Why |
|---|---|---|
| Python agent → OTel collector | OTLP-HTTP (proto over HTTP) | Standard collector ingest |
| OTel collector → CH-A `otel.otel_traces` | ClickHouse native protocol via `clickhouseexporter` | Standard exporter |
| OTel collector → Langfuse | OTLP-HTTP to `langfuse-web:3000/api/public/otel` | Langfuse's native OTel ingest |

## What this overrides

From ADR 02 (Python agent migration):

| Original (ADR 02) | Now |
|---|---|
| Python owns the `agent_ledger` writes via `clickhouse-connect`; the only writer to `multichain.agent_ledger` after Phase C | Ledger module + table deleted entirely. OTel spans replace per-step records. The ledger stops being a thing. |
| Schema for `agent_ledger` (kind enum, payload JSON, sequence counter) | All gone. `otel.otel_traces` (auto-created by `clickhouseexporter`) is the only per-step record. |

The split between data plane (Rust) and agent plane (Python) from
ADR 02 is unchanged. The "no backward compat" and "all data refreshes"
rules from `AGENTS.md` are what permit this clean replacement.

## Rationale

Five drivers, in order of weight.

### 1. Eval needs the same data; build the substrate once

Industry pattern is unambiguous: traces and evals are coupled, not
sequential. Eval probes consume per-step span data to assert
behavior ("agent called the right tool with the right args").
Building a custom event ledger first and then a parallel trace
substrate for eval would mean two formats, two writers, two
consumers, drift between them. Building OTel from day one means
Ship 2 (eval) is purely a consumer with zero new instrumentation
work.

### 2. OTel is the convergent industry standard for agent observability

OpenTelemetry GenAI semantic conventions reached consensus across
2024-2026. Major vendors ship against it (Datadog, Honeycomb, New
Relic, Logfire, Langfuse, Arize). Major frameworks emit it natively
(LangChain, CrewAI, AutoGen, Pydantic AI). Adopting the standard
means our instrumentation works against any consumer that speaks
OTel, present or future.

### 3. The ledger doesn't earn its keep at our scale

I went looking for one query the bespoke ledger does that OTel
spans don't, and could not find one. Spans either tie or beat the
ledger on every query I tested:

- "Tokens spent per session" → spans win (Pydantic AI emits
  `gen_ai.usage.*` for free; ledger would have to write a custom
  event per LLM call)
- "What was the parent of this event" → spans win (`parent_span_id`
  automatic; ledger would need a manually-maintained sequence chain)
- "Duration of the gate" → spans win (`end_ts - start_ts` automatic;
  ledger requires two events with timestamp math)
- "All retracted claims last week" → tie (simple WHERE on either)
- "Session timeline" → tie

The ledger's only "advantage" is slightly nicer SQL on typed columns
(5 characters less). Not enough to justify a parallel system with
its own writer, schema, and drift surface.

### 4. Specialized LLM UI beats unified general UI for our goals

Two paths considered for the human-facing trace UI:

**Path A (chosen): Specialized + fan-out.** Langfuse's UI has
LLM-specific affordances: prompt diff view, evaluation dataset
linkage, structured cost-per-prompt panels, agent-trace trees laid
out for LLM debugging. Trade: ~3MB/day of duplicate span data, one
extra ClickHouse container, ~256MB extra RAM.

**Path B (rejected): Unified ClickStack.** HyperDX (ClickHouse's
official OSS observability UI) reads `otel_traces` directly. True
single source of truth, half the containers, matches the 2026
"unified observability" trend. Trade: HyperDX is general
observability without the LLM-specialized affordances; weaker
brand-name recognition (ClickStack is recognized in ClickHouse
circles only).

For our specific goals (real LLM debugging value plus resume
signal in LLM engineering), Path A wins. Resume bullet specificity:
"used self-hosted Langfuse, the OSS LLM observability platform used
by Canva, Twilio, Samsara, Khan Academy" beats "used HyperDX from
the ClickStack project" in name recognition for AI/ML hiring
managers. The duplication cost is rounding error at portfolio scale,
and the architecture cleanly degrades to Path B if Langfuse ever
becomes a maintenance burden (drop Langfuse, point a HyperDX
container at the same `otel_traces` table).

### 5. Two ClickHouses mirror production posture without overengineering

CH-A holds our application data + our trace data (joinable).
CH-B holds Langfuse's internal storage. Two CH instances rather than
one shared CH:

- **Workload isolation.** Langfuse backfills (re-evaluating a dataset
  against a new model) can spike CH CPU; with shared CH our agent's
  primitive query latency would be affected. Separate CH means
  zero cross-contamination.
- **Schema isolation.** Langfuse v3 ships ~3 minor releases per week,
  some touching ClickHouse schema. With shared CH we'd risk Langfuse
  migrations interfering with our schema. Separate CH lets each
  evolve independently.
- **Mental clarity.** "Where does X data live" has a one-word answer.
- **Production-shape posture.** Real production teams running both
  an analytics warehouse and an LLM observability platform run them
  separately; sharing is a dev shortcut. Adopting the production
  shape now (at very small additional cost: ~256MB RAM, one extra
  container) avoids relearning the architecture later.

## Consequences

### Accepted

- Two ClickHouse instances in `docker-compose.yml`. CH-A existing,
  CH-B new (langfuse-dedicated). RAM cost ~256MB additional at idle;
  fits comfortably in the 24GB Oracle VM.
- Six new compose services for the Langfuse stack: `langfuse-web`,
  `langfuse-worker`, `clickhouse-langfuse`, `langfuse-postgres`,
  `langfuse-redis`, `langfuse-minio`. Plus one for the OTel
  collector. Total ~7 new services.
- Same span data lands in both CH-A's `otel.otel_traces` and
  Langfuse's CH-B; ~3MB/day at portfolio scale, zero drift risk
  because fan-out is synchronous at the collector.
- ClickHouse `multichain.agent_ledger` table dropped at cutover.
  Per AGENTS.md "all data refreshes" rule, accepted. Backup is
  one-line if we want it (`SELECT * FROM agent_ledger FORMAT
  JSONEachRow > backup.jsonl`); probably skip since the data was
  never consumed.
- `agent-service/src/agent_service/ledger/` directory deleted in
  Ship 1.
- Langfuse pinned to v3.172.1; upgrades become deliberate decisions
  rather than `latest`-tag surprises. Cost: periodic version-bump
  ritual.

### Rejected

- **Restore the bespoke ledger with the missing 7 event kinds.**
  Reinventing what OTel already standardizes; would create drift
  surface; would not give Langfuse anything to consume.
- **HyperDX / unified ClickStack pattern.** Right answer for general
  observability + cost-conscious deployments; loses to specialized
  LLM tooling for our resume + debugging-affordance goals. Revisit
  if Langfuse becomes a maintenance burden or span volume goes 100x.
- **Logfire (Pydantic SaaS).** Tightest Pydantic AI integration but
  hosted-only, less name recognition than Langfuse outside Python
  shops, vendor lock-in. Skip; Langfuse covers the same need
  self-hosted.
- **Share one ClickHouse between our app data and Langfuse.** Dev
  shortcut; failure-domain coupling, schema-migration coupling,
  workload contention under bursts. Two-CH is the production shape.
- **OpenInference convention** (Arize's richer alt to OTel GenAI).
  Pydantic AI emits OTel GenAI; switching would mean opting out of
  framework defaults for marginal attribute richness we don't need.
- **Frontend bespoke trace panel.** Replaced by Langfuse UI deep-link
  button from agent-sheet. Building an in-page trace panel later is
  cheap if we ever want it; build then.

## Implementation surface

### Python (`agent-service/src/agent_service/`)

- `otel.py` (NEW): `init_otel(service_name)` builds `TracerProvider`,
  `BatchSpanProcessor`, `OTLPSpanExporter`. Calls
  `Agent.instrument_all(InstrumentationSettings(...))`. Returns the
  module-level tracer.
- `loop_driver.py`: wrap each gate, claim emission, narrative
  emission, repeat-detection, snapshot lease, and turn-diff path in
  `with tracer.start_as_current_span(...) as span:` blocks. Set
  attributes per the span inventory in the plan. Delete the ledger
  imports + write sites.
- `primitive_client.py`: wrap `wallet_profile()` and
  `community_summary()` HTTP calls in `primitive.*` spans with
  timing + sha256-12 input/output digests.
- `main.py`: lifespan calls `init_otel()` early, before agents are
  built. Delete the lifespan ledger setup.
- `pyproject.toml`: add `opentelemetry-sdk`,
  `opentelemetry-exporter-otlp-proto-http`,
  `opentelemetry-instrumentation-fastapi`. All maintained, all OSS,
  pass AGENTS.md library bar.

### Python (`agent-service/src/agent_service/`, deletes)

- `ledger/` directory entirely (`writer.py`, `__init__.py`, related
  tests).

### Infra

- `docker-compose.yml`: add `otel-collector`,
  `langfuse-web`, `langfuse-worker`, `clickhouse-langfuse`,
  `langfuse-postgres`, `langfuse-redis`, `langfuse-minio`. Pin all
  Langfuse-stack image tags.
- `infra/otel-collector-config.yaml` (NEW): receivers
  (`otlp` http on 4318), exporters (`clickhouse` to CH-A `otel`
  database, `otlphttp/langfuse` to CH-B's Langfuse), processors
  (`batch`), service.pipelines.traces wired through both exporters.
- `.env.example`: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`,
  `LANGFUSE_INIT_*` first-boot vars, `CLICKHOUSE_LANGFUSE_PASSWORD`.
- `justfile`: `observability-smoke` recipe.

### Frontend (small)

- `frontend/src/components/agent/agent-sheet.tsx`: "view trace in
  Langfuse" deep-link button, shown after the Done frame.
- `proto/multichain/wire/agent/v1/session.proto`: add `trace_id`
  field on `AgentDone`.

### ClickHouse

- `DROP TABLE multichain.agent_ledger` once at cutover (CH-A).
- `otel.otel_traces` auto-created by `clickhouseexporter` on first
  ingest (CH-A).
- CH-B's schema entirely owned by Langfuse; we don't touch it.

## Verification

Per the plan's per-step verification (A through E in Ship 1):
- After step A: `Agent.instrument_all()` enabled, spans appear in
  the OTel collector debug log.
- After step B: every gate, primitive, claim, narrative emission
  emits its named span.
- After step C: Langfuse UI at `http://localhost:3001` shows the
  project and renders flame graphs for new turns.
- After step D: `grep -r "agent_ledger" agent-service/` returns
  zero.
- After step E: SSE Done-frame UI hang no longer reproduces.

End-to-end smoke (`just observability-smoke`):
- One known turn produces ≥10 spans in CH-A's `otel.otel_traces`.
- Same trace visible in Langfuse via its API.
- Cross-store join `otel.otel_traces JOIN multichain.node_roles`
  returns rows.
- Zero ledger writes attempted.

## References

- ADR 02 (`02-python-agent-migration.md`), the document this
  modifies (ledger ownership and existence).
- AGENTS.md sections "Library maintenance bar" (Langfuse and OTel
  collector contrib qualify; Helicone OSS in maintenance mode does
  not), "No backward compat layers" (clean ledger removal at
  cutover), "All data refreshes" (table drop without migration).
- OpenTelemetry GenAI semantic conventions
  (https://opentelemetry.io/docs/specs/semconv/gen-ai/), the
  standard our spans follow.
- Pydantic AI documentation
  (https://ai.pydantic.dev), `Agent.instrument_all()`,
  `InstrumentationSettings`, `RunResult.usage()`,
  `pydantic_evals.HasMatchingSpan`.
- Langfuse self-host documentation
  (https://langfuse.com/self-hosting/docker-compose) and their
  `docker-compose.yml` source of truth at
  `github.com/langfuse/langfuse/blob/main/docker-compose.yml`.
- ClickHouse `clickhouseexporter` for OTel collector contrib
  (https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter/clickhouseexporter).
- Industry context for the fan-out vs unified decision: ClickHouse
  ClickStack (https://clickhouse.com/clickstack) for the unified
  pattern; Langfuse adoption signals (21k+ GitHub stars, named
  customers) for the specialized-platform pattern.
- Implementation plan (kept for reference, not authoritative):
  `/Users/nabin/.claude/plans/agent-observability.md`.
