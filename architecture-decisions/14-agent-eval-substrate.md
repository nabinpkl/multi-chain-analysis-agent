# 14: Agent eval substrate (layered, framework on top, foundation we own)

This document records the decision to build Ship 2's eval system as
a four-layer stack where the schema, probes, and runner are ours,
and the framework (pydantic_evals) sits as a thin adapter on top.
The decision rejects three alternatives we seriously evaluated:
adopt a framework wholesale, adopt no framework at all, or stay
ambiguous and decide later.

## Status

Accepted, 2026-05-04. **Amended 2026-05-05**: the planned Layer 4
pydantic_evals adapter was dropped after deeper investigation. See
"2026-05-05 addendum" below. Layers 1-3 stand unchanged.

## 2026-05-05 addendum: dropping the Layer 4 framework adapter

After Layers 1-3 landed and the smoke suite passed 6/6 against the
live stack, we revisited Layer 4 (pydantic_evals adapter) before
implementing it. Two findings reversed the decision:

1. **`HasMatchingSpan` / `SpanQuery` is in-process-only.** The
   feature that justified the framework on top, span-tree querying
   beyond what hand-written CH SQL gives us, captures spans by
   registering a `SimpleSpanProcessor`+`_ContextInMemorySpanExporter`
   on the local `TracerProvider` and scoping them via `contextvars`
   to the `task()` callable. Source:
   `pydantic_evals/otel/_context_in_memory_span_exporter.py` on
   `pydantic-ai@main`. Our spans flow agent process →
   otel-collector → ClickHouse, across processes. The eval runner
   cannot register a span processor on the agent's TracerProvider.
   The open feature request to "create SpanTree from historical
   data" (`pydantic-ai#3946`, open as of 2026-05-05) confirms this
   is a known gap, and even when it lands it targets logfire's API,
   not arbitrary OTel backends. So the SpanQuery primitive is
   structurally unreachable for our architecture.

2. **The remaining adapter benefits don't justify a runtime
   dependency.** Without SpanQuery, what pydantic_evals would still
   give us reduces to: `LLMJudge` evaluator, `generate_dataset`
   case authoring, `ConfusionMatrix`/`PrecisionRecall` aggregate
   metrics, and a parallelism model tied to in-process tasks. Each
   is additive and reachable as a native probe kind or a small CLI:
   - `LLMJudge` → new `llm_judge` `ProbeKind`, ~150 LOC, reuses our
     dispatch + persistence.
   - `generate_dataset` → standalone `just eval-gen` CLI; a custom
     ~50 LOC implementation or wrapping pydantic_evals's function
     standalone (no runtime coupling either way).
   - Aggregate metrics → can be added to `summarize_run` if a real
     case load makes them load-bearing.
   - Parallelism → still deferred behind the
     rate-limit-per-provider question regardless of framework.

   Each path is independent and small. Bundling them under a
   framework adapter trades that independence for an upgrade-path
   coupling we don't need.

3. **Industry pattern, recheck (2026-05-05).** Anthropic's
   2026-01-09 post says "pick a framework that fits your workflow,
   then invest in the cases" -- not "always adopt one". LangChain
   2026-02-12 markets LangSmith as framework-neutral observability;
   "many customers don't use our open source frameworks but rely on
   LangSmith for observability and evals". Vercel's recent
   AGENTS.md-vs-skills evals post and Arize's MCP-vs-CLI evals post
   both describe in-house probe writing on top of platform-managed
   trace storage. The pattern is layered: platform for storage and
   UI (we have CH + Langfuse, the ClickStack-shaped equivalent),
   custom probes for semantics. We are already on the typical 2026
   pattern; the framework adapter would have been redundant.

The earlier ADR rationale (above) treated the OTel-events-driven
2026-04-21 pydantic_evals shipment as evidence that the framework
already met us where we were. It turns out that shipment is
consumer-side OTel ingestion via logfire's tracer integration, not
"pull from arbitrary OTel backend like CH". That was the missed
detail.

**Effect on Layers 1-3:** none. The probe dispatch, schema, and
runner were all designed framework-agnostic on purpose. Dropping
Layer 4 simply means the seam exists and stays empty.

**Effect on Layer 1's `FrameworkAdapter` Literal:** narrowed from
`Literal["pydantic_evals", "framework_free", "inspect_ai"]` to
`Literal["framework_free"]`. AGENTS.md "no dead optionality" applies
to Literals too: a value with no implementation doesn't earn its
keep. The single-arm dispatch in `_select_adapter` stays as a
function so a future adapter that *does* fit our architecture can
slot in at one site, but until that day arrives the type checker
sees one option.

**Effect on the four invariants:** unchanged. They were always the
substance; the framework layer was scaffolding.

**Replacement work:** two narrow tickets supersede the dropped
Layer 4 ticket (#24, closed):

- `llm_judge` probe kind (subjective grading by an LLM with a
  rubric). Net new probe class; reuses existing dispatch and
  persistence.
- `generate_cases` CLI (LLM-authored YAML expansion from a
  description). Standalone tool; not wired into the runner.

**Trigger to re-add a framework adapter:** if (a) `pydantic-ai#3946`
ships AND that feature reads from arbitrary OTel backends (not
logfire-only), OR (b) a different framework appears that natively
ingests OTel from CH/Tempo/etc. with a query DSL more expressive
than our hand-written probes, then revisit per the same dating
rule (`<=6mo` evidence) that drove this addendum.



## Problem

Ship 1.5 of agent-observability (ADR 13) leaves us with a rich OTel
trace substrate: `mcae.*` domain spans (gates, claims, primitives,
turn root, snapshot, narrative) plus `gen_ai.*` spans auto-emitted
by pydantic_ai (LLM calls, tool dispatch, agent runs), all landing
in `otel.otel_traces` and Langfuse v3. Ship 2 needs to turn this
substrate into an eval system: assert that the trust-model holds
across a corpus of cases, run regressions on every meaningful
change, and produce results we can reason about long after the
specific run.

Three forks-in-the-road shaped the design below.

1. **Pick a framework wholesale, or build framework-free?** The
   honest middle. Framework wholesale fuses our trust-model probe
   semantics with someone else's release schedule; framework-free
   leaves us reinventing scaffolding (case loading, dataset diffing,
   summary tables) that we don't need to own. Layered with an
   adapter is the deliberate middle.

2. **Which framework, if any, on top?** pydantic_evals. It already
   ships in our dependency tree via `pydantic_ai`; its 2026-04-21
   "online evaluation via OpenTelemetry events" shipment maps
   directly to our existing OTel pipeline; and it does not couple
   us to Logfire SaaS the way earlier framings of pydantic_evals
   feared.

3. **What lives in our schema, what lives in the framework?** Probe
   semantics, case shape, and result shape are ours; case dataset
   loading, evaluator scaffolding, and summary-table rendering are
   the framework's. The seam between them is one ~80 LOC adapter
   file we control.

## Decision

A four-layer stack. Dependency arrows point downward; lower layers
know nothing about higher ones.

```
┌────────────────────────────────────────────────────────────────┐
│ Layer 4: Framework adapter (THIN, SWAPPABLE)                   │
│   agent_service/evals/adapters/pydantic_evals_adapter.py       │
│   - Translates EvalCase  pydantic_evals.Case                  │
│   - Translates ProbeSpec  pydantic_evals.Evaluator subclass   │
│   - Translates pydantic_evals output  ProbeResult             │
│   ~80 LOC. The only file that changes if we swap frameworks.   │
└────────────────────────────────────────────────────────────────┘
                              
┌────────────────────────────────────────────────────────────────┐
│ Layer 3: Runner (FRAMEWORK-AGNOSTIC)                           │
│   agent_service/evals/runner.py                                │
│   - Loads YAML cases  list[EvalCase]                          │
│   - Invokes /agent/ask, captures trace_id                      │
│   - Dispatches to probes via Layer 4 adapter                   │
│   - Persists ProbeResult JSON, computes summary                │
│   - `just eval` target. Pytest-driven optional.                │
└────────────────────────────────────────────────────────────────┘
                              
┌────────────────────────────────────────────────────────────────┐
│ Layer 2: Probes (PURE FUNCTIONS, OURS FOREVER)                 │
│   agent_service/evals/probes/                                  │
│     has_matching_span.py                                       │
│     tool_called_with_args.py                                   │
│     claim_grounded_in.py                                       │
│     gate_passed.py                                             │
│     span_latency_p50_under.py                                  │
│   Each: (ProbeSpec, trace_id, CHClient)  ProbeResult          │
│   No framework imports. Reads otel.otel_traces directly.       │
│   Asserts against mcae.* and gen_ai.* attrs.                   │
└────────────────────────────────────────────────────────────────┘
                              
┌────────────────────────────────────────────────────────────────┐
│ Layer 1: Schema (CANONICAL, OURS FOREVER)                      │
│   agent_service/evals/schema.py                                │
│   EvalCase, ProbeSpec, ProbeResult, RunMetadata                │
│   Pydantic models. JSON-serializable. No framework types.      │
│   Cases live in YAML; results live in JSON on disk and (later) │
│   in a ClickHouse projection alongside otel.otel_traces.       │
└────────────────────────────────────────────────────────────────┘
```

## Rationale

### Why not adopt a framework wholesale

The 2026-01-09 Anthropic engineering post on agent evals
(`anthropic.com/engineering/demystifying-evals-for-ai-agents`,
verified date) names five frameworks (Harbor, Braintrust, LangSmith,
Langfuse, Arize) neutrally and tells readers: "It's often best to
quickly pick a framework that fits your workflow, then invest your
energy in the evals themselves." Anthropic itself describes building
internal transcript-review tooling and dedicated evals teams; they
do not standardize on one framework.

The pattern in the field, observed across credible 2026 sources:
serious teams who care about eval semantics own their substrate.
Frameworks are scaffolding for teams that haven't yet developed
strong eval-content opinions. By Ship 1.5 we have those opinions:
the two-gate trust model, the structural value gate, the
`mcae.claim.source_kind` anchor, the `mcae.gate.version` pin. These
are the substance. Embedding them in someone else's framework type
hierarchy hides them.

Wholesale adoption of a framework also fuses probe semantics with
the framework's release schedule. A pydantic_evals 2.x breaking
change to the `Evaluator` interface would force a rewrite of every
probe class; the rewrite would touch trust-model code, not just
adapter code. That violates the AGENTS.md "no adapter bridging
two-things-that-should-be-one-thing" rule applied to versions: the
framework's evaluator interface and our trust-model probe semantics
should not be the same artifact.

### Why not build framework-free

Framework-free has real appeal. Probes are pure functions; the
runner is ~30 lines; total LOC drops by ~80. The substrate becomes
something we could publish standalone.

But framework-free leaves us reinventing scaffolding we don't need
to own:
- Per-run summary tables and pass/fail rendering.
- Dataset diffing across runs (regression detection).
- Result persistence patterns that are already idiomatic in
  pydantic_evals.
- Online OTel-event-driven evaluation (pydantic_evals shipped this
  2026-04-21; building it from scratch is non-trivial).

For each of these, pydantic_evals' implementation is reasonable.
Refusing to use it on principle is the inverse error of wholesale
adoption: building everything because we want to build something.

The decisive consideration: the cost of pydantic_evals updating in
ways we don't want is bounded to one ~80 LOC adapter file, and the
cost of pydantic_evals dying entirely is also bounded to that one
file (rewrite as framework-free, ~40 LOC). The insurance is cheap;
paying it is right.

### Why pydantic_evals specifically

Three reasons, all from CURRENT (2026, dated) sources:

1. **Already in our dependency tree.** `pydantic_ai>=1.0` (which we
   pin as `pydantic-ai-slim[openai]>=1.0`) brings `pydantic_evals`
   as a sub-package. Zero new dependency surface.

2. **OTel-events-driven evaluation, shipped 2026-04-21** (commit
   "Online evaluation via OpenTelemetry events #5125", verified via
   GitHub API on 2026-05-05). This is the feature that earlier
   research mistakenly claimed pydantic_evals lacked. It reads our
   existing OTel pipeline directly without requiring Logfire as the
   trace backend. The earlier critique that pydantic_evals is
   "Logfire-coupled" no longer holds.

3. **Active maintenance.** Last release `v1.90.0` on 2026-05-05,
   evals subdirectory commits within the last week (verified via
   GitHub API on 2026-05-05). Not abandoned, not bot-driven,
   passes the AGENTS.md library bar.

The earlier "small surface, niche tool" framing was based on the
wrong question: "is pydantic_evals an industry standard?" The right
question is: "is pydantic_evals a competent renderer of cases and
evaluators that we can use without giving it our trust-model
semantics?" The answer to the right question is yes.

### Why this is the right middle ground

The four invariants this design protects:

1. **A case is data, not code.** YAML, loadable into a pydantic
   type, stable IDs.
2. **A probe is a predicate over an OTel trace.** Pure function:
   `(ProbeSpec, trace_id, CHClient)  ProbeResult`. No framework
   types in the signature.
3. **A probe result is a structured artifact.** Persistable as JSON,
   queryable in ClickHouse, schema independent of any tool.
4. **The agent under test is invoked exactly the way production
   invokes it.** `/agent/ask` over HTTP, real OTel pipeline, real
   `mcae.*` and `gen_ai.*` spans. No in-process shortcuts.

Layers 1 and 2 are these invariants made executable. Layer 3 is the
orchestration that satisfies invariant 4. Layer 4 is the seam where
pydantic_evals' scaffolding meets our invariants. The adapter
translates in both directions: our `EvalCase`  pydantic_evals
`Case`, pydantic_evals `EvaluatorOutput`  our `ProbeResult`. The
framework never sees a probe's logic; the probe never sees the
framework.

This protects against three failure modes:

- **Framework upgrade we don't want.** Re-implement Layer 4 only;
  Layers 1-3 untouched.
- **Framework deprecation or replacement.** Rewrite Layer 4 as a new
  adapter (Inspect AI, framework-free, etc.); cases and probes
  unchanged.
- **Probe semantics evolution.** Add a probe kind to the schema's
  `Literal`, write a new probe file in Layer 2, the adapter picks
  it up via `probes.dispatch(kind)`. No framework migration.

### What we explicitly accept giving up

- A new pydantic_evals feature lands and we want it: the adapter
  needs an update to surface it through our schema. Bounded to one
  file, half-day work.
- A pydantic_evals user joining the project doesn't see idiomatic
  pydantic_evals code. They see our adapter and our probe pattern,
  which uses pydantic_evals via the adapter. Acceptable trade for
  probe semantics readable in ~30 lines without framework
  prerequisites.
- We carry a small abstraction tax (~130 LOC) compared to the
  direct-use shape. The tax pays for swap insurance, probe
  testability in isolation, and the publishability of the substrate
  (relevant to issue #19, the structural value gate writeup
  candidate).

## Consequences

### Implementation

Ship 2 day-1 work:
- `agent_service/evals/schema.py` (~80 LOC, all pydantic models, no
  logic)
- `agent_service/evals/probes/` with 5-7 pure-function probe
  modules (~150 LOC total)
- `agent_service/evals/adapters/pydantic_evals_adapter.py` (~80
  LOC, single seam)
- `agent_service/evals/runner.py` (~50 LOC, orchestration)
- `agent_service/evals/cases/*.yaml` seed corpus (~30 LOC YAML)
- `just eval` target

### Operational

- Eval runs are out-of-band, not CI-gated. `just eval` from a
  developer shell or scheduled run.
- `AGENT_RUN_TYPE=eval` environment variable is set so eval traces
  carry `mcae.run.type=eval` and filter cleanly in Langfuse and
  ClickHouse.
- Each run produces a `RunMetadata` JSON + per-`ProbeResult` JSON
  files under `evals/runs/<run_id>/`. Optional ClickHouse projection
  (`evals.probe_results`) can be added later for cross-run queries
  without touching the schema.

### Long-term

- The schema (Layer 1) is the publishable artifact. If we ever do
  the issue #19 writeup, the post describes our substrate (cases as
  data, probes as predicates over spans, the trust-model anchors)
  and treats the framework as an implementation detail.
- The probe set is the moat. New trust-model invariants
  (sql_explore source_kind, future structural-gate refinements) get
  expressed as new probe kinds, which extend the schema's `Literal`
  and add files to Layer 2. No framework migration ever needed for
  trust-model evolution.

## Trigger conditions to revisit

- pydantic_evals introduces a feature we want that does not map
  cleanly to our schema (e.g. multi-trace correlation, graded scoring
  DAGs, interactive case authoring). At that point we either extend
  our schema to model the concept generically or accept that the
  feature is reachable only through Layer 4 and not portable across
  adapters.
- pydantic_evals stagnates or pivots in a direction incompatible
  with our use. Rewrite Layer 4 as a different adapter (Inspect AI
  is the most likely replacement; framework-free is the fallback).
- Anthropic, OpenAI, or another credible 2026+ source publishes an
  agent-eval pattern that materially differs from this design.
  Revisit on the basis of CURRENT (<=6mo) evidence per the
  AGENTS.md research-dating rule.

## References

- ADR 13: Agent observability foundation (OpenTelemetry + Langfuse).
  Provides the trace substrate this ADR consumes.
- AGENTS.md: library acceptance bar (pydantic_evals passes),
  research-dating rule (load-bearing for ruling out stale industry
  claims), no-adapter-bridging-two-things rule (drives Layer 4 as
  the deliberate seam, not accidental glue).
- Anthropic Engineering, "Demystifying evals for AI agents",
  2026-01-09. Source for "pick fast, invest in eval content" and
  for the observation that no framework has runaway adoption.
- pydantic_ai commit "Online evaluation via OpenTelemetry events
  #5125", 2026-04-21. Source for pydantic_evals being OTel-native
  in current shape.
