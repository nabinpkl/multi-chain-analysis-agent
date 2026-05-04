# 01: Chain analysis agent, overview

A read-only LLM agent that answers analytical questions about a live
Solana transaction graph and the historical edge warehouse. Anonymous
users, public data, hard cost ceiling per principal, every claim
provenance-attached. This document is the entry point; each subsequent
file goes deep on one layer.

## Problem statement

The system already maintains:
- A live in-process graph (`GraphState`) with windowed views, Louvain
  communities, MPC scoring, role classification (see
  `architecture-decisions/05-09-*` for the supporting analytics work).
- A ClickHouse warehouse with every ingested edge (versioned via
  `ReplacingMergeTree`), partitioned by day.

What the system lacks: a way for an analyst to ask questions in
natural language and get answers grounded in that data, where the
answer is checkable, the cost is bounded, and the surface is safe to
expose to anonymous traffic.

A naive "let an LLM write SQL" approach fails on three axes
simultaneously: cost (unbounded query-time), correctness (hallucinated
schema), and security (prompt injection from on-chain text). The
design here is a structured alternative.

## Threat model

Public chain data eliminates the usual enterprise threats (data leak,
multi-tenant isolation, customer billing). The threats that remain:

1. **Compute exhaustion.** A single bad query against the warehouse
   pegs the deployment node. Defended by typed primitives with
   pre-flight cost gating.
2. **Token / API-spend exhaustion.** Agent in a loop burns budget
   silently. Defended by multi-axis budget buckets per principal.
3. **Confident wrong claims.** Agent asserts something untrue with no
   trace. Defended by mandatory provenance attached to every claim.
4. **Data-borne prompt injection.** On-chain memo fields, SPL token
   names, and (eventually) third-party wallet tags are user-authored
   text the agent reads. Defended by three layers: structural
   separation, tool-result-as-data, output policy.
5. **Adversarial graph structure.** A motivated actor shapes their
   on-chain activity to fool the classifier or the agent reading
   classifier output. Out of scope for v0; mitigated by the
   provenance trail (auditable claims) rather than by detection.

This threat model maps directly to the OWASP Top 10 for LLM
Applications:
- LLM01 Prompt Injection -> phase 03
- LLM02 Insecure Output Handling -> phase 03 (output policy + UI
  rendering rules)
- LLM04 Model Denial of Service -> phase 05 (budget buckets)
- LLM06 Sensitive Information Disclosure -> N/A (public data)
- LLM07 Insecure Plugin Design -> phase 02 (typed primitives, no SQL
  authoring, no code execution)
- LLM08 Excessive Agency -> phase 02 (read-only) + phase 05 (budget)
- LLM09 Overreliance -> phase 03 (provenance) + phase 06 (eval)

## Architectural shape

Single Rust process. The agent runs as a tokio task alongside the
existing ingest and analytics tasks. SSE channel from agent to
frontend. Same ClickHouse instance, dedicated read-only role.

```
                     +-----------------------+
                     |  Frontend (Next.js)   |
                     |  Sidebar + claims UI  |
                     +-----------+-----------+
                                 | SSE (Claim slices)
                                 v
+----------------+        +------+--------+        +---------------+
|  GraphState    |<------ | Agent runtime |------> | LLM provider  |
|  (live)        |  read  |  - planner    |        |  (via rig,    |
+----------------+        |  - executor   |        |   swappable)  |
                          |  - policy     |        +---------------+
+----------------+        |  - budget     |        +---------------+
|  ClickHouse    |<------ |  - ledger     |------> | Output policy |
|  (warehouse)   | read   +---+-----------+        | (cheap model) |
|  read-only role|            |                    +---------------+
+----------------+            |
                              v
                     +--------+---------+
                     |  Action ledger   |
                     |  (ClickHouse,    |
                     |   append-only)   |
                     +------------------+
```

The agent never sees a database connection, never authors SQL, never
holds a write credential. It composes typed primitives. Every action
is logged.

## Invariants

Six locked-in design constraints. Each subsequent file serves at least
one. A change that violates an invariant requires re-opening this
document, not a silent edit.

| # | Invariant | Primary file |
|---|-----------|--------------|
| 1 | Read-only typed primitives over GraphState + ClickHouse | 02 |
| 2 | Three-layer untrusted text defense | 03 |
| 3 | Provenance-attached claims streamed as complete slices | 03 |
| 4 | Anonymous principal model (cookie + truncated IP) | 05 |
| 5 | Cost-as-rate-limit (multi-axis budget buckets) | 05 |
| 6 | Action ledger + eval suite | 04, 06 |

## Out of scope

These would be theater in this context. The seam is left clean so
they could be added in a different deployment, but adding them here
would not defend anything real.

- Multi-tenant isolation (no second tenant)
- Role-based access control (no second user role)
- Approval workflows (read-only system; nothing needs human approval)
- PII redaction (no PII on the chain)
- Encryption at rest (chain data is public)
- Browser fingerprinting (privacy cost exceeds defense value)
- Service-to-service auth (single process)

## Phase 0: scaffolding decisions (resolved before any code)

These four questions block every other phase. Recorded as decisions
below; revisit only if a later phase reveals one was wrong.

### D-1: Where the agent runs

**Decision:** same Rust process as ingest + analytics, separate tokio
task.

**Rationale:** live primitives need cheap access to `GraphState` (an
`Arc<RwLock<...>>` already in process). A separate service would add
a hop and require the live graph to be exposed via an internal RPC,
duplicating the existing analytics-task pattern. The cost-amplification
risk that motivates a separate process (agent runaway pegging the
host) is already addressed by the budget buckets in phase 05.

### D-2: Provider-agnostic LLM client + pinned model identifiers

**Decision:** the LLM client is `rig` (provider-agnostic Rust crate
with native support for ~20 providers). Provider and model are
selected at deploy time via configuration, not hard-coded in the
loop. Within that configuration, exact model identifiers are pinned
in Rust constants per environment. No floating "latest" aliases.

Two model slots:
- **Primary reasoning model.** The high-capability model the loop
  uses for the main turn-by-turn reasoning.
- **Output-policy model.** The cheapest model variant available
  from the configured provider that still parses structured input
  and produces structured output reliably.

Updating either the provider or a pinned model identifier is a
code change with an eval-suite gate (phase 06).

**Rationale:** floating aliases produce silent regressions on
vendor updates; pinning forces every model change through review.
Provider abstraction (via rig) keeps the loop usable across
vendors so a pricing or capability shift does not strand the
codebase on a single vendor. The agent loop, primitive layer,
prompt assembly, output policy, ledger, and cost framework are all
provider-neutral; vendor-specific shape lives behind rig's
abstraction.

### D-3: Conversation surface

**Decision:** sidebar overlaid on the existing graph page. The live
graph is visible while the agent answers. Claims render in the
sidebar; provenance refs that point at entities currently in the
live window highlight on the same canvas, refs that point at
historical entities open a self-contained subgraph modal, pure
aggregates render as structured cards. The user stays on one page;
the surface adapts to what the claim is about. See D-5 for the
source split that drives this.

**Rationale:** the graph is the analyst's working memory. Splitting
the analysis into a separate page would force context switching that
the streaming-claim-slices design (phase 03) is meant to eliminate.
A modal for historical results communicates "this is isolated, not
the live canvas" without forcing a route change.

### D-4: Claim wire format

**Decision:** typed via `ts-rs` from a Rust `Claim` struct. Provenance
is a tagged enum referencing stable identifiers
(`NodeIdx`, `EdgeId`, `community_id`, `block_time` ranges). UI
renderer turns each ref into an interactive chip.

**Rationale:** generated bindings keep frontend + backend in sync (the
existing `AnalyticsBatch` pattern). Tagged provenance enums let the UI
render different ref types differently without a switch on string
keys.

The full `Claim` shape lives in `03-agent-loop-and-injection-defense.md`.

### D-5: Three data sources, one agent, surface from provenance

**Decision:** the agent has three primitive families, distinguished
by data source: **live** (read `GraphState`), **warehouse** (read
ClickHouse), **external** (third-party tag sources, deferred to
phase 07). One agent loop composes across all three. The render
surface for a claim is derived from the shape of its provenance
refs, not a mode toggle:

- Provenance refs to wallets/edges/communities currently in the
  live window highlight on the live graph.
- Provenance refs outside the live window (or carrying an absolute
  `TimeRange`) render in a subgraph modal alongside the claim.
- Pure aggregate refs (no entity refs) render as a structured card.
- External-source refs carry an inline source attribution
  ("per helius.xyz") next to the chip.

**Rationale:** conflating live and historical at the page level
papers over the real distinction. Each source has different cost
characteristics (phase 05), different temporal semantics, and
different rendering needs. A single agent that picks per question
can answer mixed questions ("compare last hour to right now")
without forcing the user to pick a mode. The provenance-derived
surface keeps the wire format declarative; the renderer is the only
place that maps shapes to surfaces.

### D-6: Disambiguation principle

**Decision:** push ambiguity to the user-facing edge; eliminate it
at the agent's edge. The seam is structured. Three layers do the
disambiguation work, in decreasing order of authority:

1. **Frontend context block.** The user's question is wrapped with
   structured state describing what they are looking at (current
   live window, focused node, selection, current time). The block
   is JSON-typed in the prompt assembly, separate from user-authored
   text, so it cannot be confused with content the agent must defend
   against (phase 03).
2. **Mandatory typed time scope.** Primitives with temporal
   semantics take a required `TimeScope` argument
   (`Live | Range { from, to }`). The agent cannot call a temporal
   primitive without committing to a frame; the choice is auditable
   in the action ledger (phase 04) instead of hiding inside model
   reasoning.
3. **Tool descriptions with routing examples.** Each primitive's
   description teaches when to pick `Live` vs `Range` from question
   patterns. Vendor function-calling guidance across providers
   converges: rich descriptions with examples beat clever system
   prompts.

The model's judgment is the residual layer, not the primary one,
applied only when the prior three leave ambiguity. The system
prompt's disambiguation rule for that residue is "default to live
and state the frame in the claim ('answering for the current
60-second window; ask about a specific time for historical depth')".

**Rationale:** common failure modes are either rigid query DSLs
(no flexibility for the user) or LLM-disambiguates-from-raw-text
(no auditability, prompt-injection surface widens). Structuring
the seam preserves natural-language input while making the agent's
decisions explicit at the type level. Misroutes are bounded: a
`Live` call against an entity outside the window returns
"not in current window" cheaply; a `Range` call against a small
window costs little. Drift telemetry (phase 04) catches recurring
misroutes; descriptions tighten in response.

### D-7: Two modes (reactive + proactive) over one core

**Decision:** the agent runs in two modes that share every
underlying layer (primitives, claim format, ledger, output policy,
cost framework). **Reactive** answers user questions in the
sidebar. **Proactive** surfaces unprompted observations to a
separate "pulse" panel by running an LLM analyst over a structured
signal stream produced by deterministic Rust extractors. Pulse
claims feed back into the reactive `ViewContext` so the user can
interrogate them without ambiguity ("tell me more about that
wallet you just flagged"). Detail in `08-proactive-pulse.md`.

**Rationale:** the high-value observations in a busy graph combine
multiple signals in ways no hand-written rule encodes (an MPC
cluster forming + members in last hour's top-10 + one tagged
yesterday). Two failure shapes were rejected:

- **Hardcoded watchers** catch only what their author thought of;
  every interesting combination cannot be enumerated; maintenance
  scales quadratically as new signal classes interact with old.
- **Pure LLM scanning** of raw graph state is too expensive,
  fixates on surface features, and lacks auditable coverage.

The split is two-layer: cheap deterministic extractors generate a
dense typed signal stream (full coverage, no LLM cost); an LLM
analyst combines signals on a slower cadence and emits hedged,
provenance-attached claims. Proactivity is not hardcoded; the agent
is the layer that does the combining, which is where pretrained
pattern noticing earns its keep. Bounded by a `system` principal
in the same cost framework; degrades gracefully on budget
exhaustion to a deterministic structured summary.

## Phase index

Each row links to a self-contained design document. Phases are
mostly orderable but not strictly serial; the dependency notes show
where flexibility exists.

| Phase | File | Depends on | Status |
|-------|------|------------|--------|
| 02 | Typed primitive layer | none | not started |
| 03 | Agent loop + injection defense + claim slices | 02 | not started |
| 04 | Action ledger | none (parallel with 02) | not started |
| 05 | Anonymous principal + cost rate-limiting | 04 (writes through ledger) | not started |
| 06 | Evaluation suite | 02, 03, 04 | not started |
| 07 | Polish + analyst surfaces | 02, 03, 04, 05, 06 | not started |
| 08 | Proactive pulse (signal stream + analyst) | 02, 03, 04, 05; eval extension lands with 06 | not started |

## Working with this document set

Each phase file is structured the same way:
1. **Problem.** What the phase is solving and the failure modes if
   it's missing.
2. **Industry standards.** Real prior art, specs, and patterns this
   phase aligns with.
3. **Open questions.** Decisions still owed before implementation.
4. **Approach.** The design committed to.
5. **Implementation surface.** Concrete file layout, types, and
   mechanics.
6. **Verification.** How to confirm the phase landed correctly.
7. **Resume prompt.** A line you can paste into chat to load context
   and start work on this phase.

Do not edit a phase doc to record runtime decisions made during
implementation; record those in this overview's `## Decisions log`
below so the cross-cutting picture stays in one place. Phase docs
describe the target state.

## Decisions log (append-only)

Format: `YYYY-MM-DD :: <decision identifier> :: <one-paragraph
rationale>`. Reference an earlier decision by id when overriding.

2026-05-03 :: agent-plane-to-python (overrides D-1, D-2, partially D-4) :: Split the agent runtime out of the Rust process into a Python service on `:8003` running Pydantic AI; Rust on `:8002` keeps the data plane (ingestion, graph window, primitive compute, snapshot lease). D-1's single-process argument was load-bearing for primitive compute (still in process) but never for the LLM loop, where turn latency is dominated 99.9% by the model call and a localhost JSON hop is invisible. D-2's `rig` choice is gone; no maintained Rust LLM client passes `AGENTS.md`'s bar. D-4's `ts-rs` source for `Claim` is gone; wire types now live in protobuf and codegen to Rust + Python + TS. D-3, D-5, D-6, D-7 unchanged. The six locked invariants in this document are unchanged. Full rationale in `architecture-decisions/13-python-agent-migration.md`.

2026-05-04 :: agent-observability-foundation (modifies 2026-05-03) :: Lay the agent observability substrate as OpenTelemetry spans (single source of truth), with Pydantic AI's built-in `Agent.instrument_all()` providing LLM/tool spans and a domain-span layer we add for gates, claims, and primitive calls. Drop the `agent_ledger` table and module entirely; spans replace it one-for-one at the same call sites. OTel collector fans out to two ClickHouse instances: CH-A (existing data plane, holds `multichain` + a new `otel` database for joinable analytics) and CH-B (new, dedicated to self-hosted Langfuse v3.172.1). Langfuse chosen over HyperDX/ClickStack for LLM-specialized UI affordances and resume signal (21k+ stars, used by Canva/Twilio/Samsara/Khan Academy); chosen over Logfire to avoid SaaS lock-in. Eval framework deferred to a follow-up ship; substrate is designed so eval probes plug in via `pydantic_evals.HasMatchingSpan` against the same trace data with zero new instrumentation. The "Three-layer cake" architecture (telemetry → persistence → consumers) and two design rules (telemetry doesn't know about consumers; persistence doesn't know about business meaning) are the load-bearing principles. This decision modifies 2026-05-03's "Python writes the agent ledger via clickhouse-connect" sub-claim; the ledger goes away. Full rationale in `architecture-decisions/14-agent-observability.md`.

## References

- OWASP Top 10 for Large Language Model Applications, 2025 edition.
- Vendor function-calling specifications: OpenAI function calling,
  Anthropic tool use, Google Gemini function calling. Convergent
  shape (typed function-calling with JSON-schema-validated args).
- Vendor prompt-injection mitigation guidance (Anthropic, OpenAI,
  Microsoft, NVIDIA): all converge on layered defense (structural
  separation, system-prompt rules, output filtering).
- `rig` crate documentation (Rust LLM client with provider-native
  abstractions across ~20 providers).
- Model Context Protocol (MCP) specification (open protocol for
  tool exposure to LLM clients).
- ClickHouse documentation: `max_execution_time`,
  `max_rows_to_read`, `EXPLAIN ESTIMATE`, read-only user roles.
- W3C, Server-Sent Events specification.
- `ts-rs` crate documentation (existing project standard for typed
  wire bindings).
