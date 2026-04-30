# 00: Build order

Sequencing for the chain-analysis-agent. The phase docs (01-08)
describe target state per layer. This file says in what order to
build, what each ship cuts across, and what discipline keeps the
cuts safe.

This is one file by design. The phase docs hold "what to build";
this holds "in what order, with what stubs". If it outgrows a
single-pass read, split into a folder; for now, narrow is correct.

## Why a build-order document at all

The phase docs read as atomic layers because they describe target
state per concern (primitives, loop, ledger, cost, eval). Read
literally as a build order, they imply ~5 weeks before a callable
agent exists.

The right shape is to ship a thin vertical slice early, with every
architectural seam in the correct shape from ship 1, and thicken
the implementations across subsequent ships. This file captures
that plan.

## Principle: seams correct from ship 1, implementations grow

The discipline that makes vertical slicing: wire formats and seam
shapes are committed from the first ship; implementations behind
those seams grow across ships.

What this means concretely:

- `Claim`, `ProvenanceRef`, `TimeScope`, `LedgerEvent`,
  `ViewContext`, `AgentRequest`, `Signal` (and friends) are the
  full typed structs the phase docs describe, exported via ts-rs,
  in ship 1. Not stubs to be promoted later.
- The agent loop writes ledger events from ship 1. Cost columns
  carry zero until ship 4; the rows exist.
- The output policy exists as a function from ship 1, returning
  `Approved` for everything until ship 2 makes it real.
- The cost framework exists as a no-op `is_principal_allowed` from
  ship 1; ship 4 implements buckets without changing the call sites.
- The primitive registry exists with one primitive in ship 1; later
  ships register more without touching the registry shape.

What we never do: change a wire format mid-stream. If a wire
format was wrong, it gets fixed before the next ship lands by
revisiting the phase doc and updating in the same ship that
introduces the consumer that exposed the problem. There are no
"v0 fields" that get removed later.

## Ships

Each ship is shippable: the system runs, just less defended or
less capable than the next. Each ship's verification is the
relevant subset of the phase docs' verification sections.

### Ship 1: skeleton end-to-end (target: 1 week)

**Scope: every layer touched, each at minimum.**

Phases involved: 02 thin, 03 thin, 04 thin, 05 stub, 06 none.

In:
- Type files first (write the structs before the logic):
  `TimeScope`, `ProvenanceRef`, `Claim` (with `Profile` variant
  only for now, but the enum exists), `ClaimKind`,
  `LedgerEventKind`, `LedgerEvent`, `ViewContext`, `AgentRequest`.
  All `Serialize + Deserialize + TS`. ts-rs exports generate; the
  frontend imports the generated types and the build passes.
- LLM client: `rig` pinned in `Cargo.toml` with the selected
  provider feature enabled. Provider + model identifiers in a
  config module; loop talks to `rig`'s provider-neutral abstraction
  per D-2.
- Phase 02: one primitive, `wallet_profile` with `time_scope: Live`
  only. Registry shape supports more.
- Phase 03: ReAct loop with one round trip on `rig`'s `Agent`;
  system prompt v1 (const &str with version tag); prompt assembly
  with `<context>` block; Layer 1 (`<external_data>` wrap) and
  Layer 2 (`tool_result` role) in place; Layer 3 = pass-through
  approval.
- Phase 04: ledger writer wired into every event point. ClickHouse
  table created and migrated. Replay function works. `cost_relevant`
  and cost units = 0.
- Phase 05: stub. `is_principal_allowed(_) -> true`. Session id
  issued. Principal hashing not yet computed.
- Frontend: agent sidebar component, claim renderer for the
  `Profile` `ClaimKind`, SSE subscription, provenance chip click
  routes to live-graph focus (using existing `use-raw-stream`
  slot map).

Stubbed and why:
- Output policy is pass-through. Why: layer 3 exercises a separate
  model call, which is a unit of complexity (cost, latency, prompt
  authoring) that warrants its own ship. The seam exists.
- Budget is always-allow. Why: bucket math + principal hashing +
  pre-flight reservations is its own unit. The seam exists.
- Eval suite is absent. Why: no value before there's something
  worth evaluating; first eval questions land with ship 6.
- All primitives except `wallet_profile` are unwritten. Why: one
  primitive proves the registry, the dispatch, the type round-trip,
  and the ledger writes. Adding more is mechanical from there.

Deliverable: ask the agent to profile a wallet visible on the
live graph; see a typed `Claim` stream to the sidebar; click a
provenance chip and the live graph focuses on the wallet; query
the ledger and see the full session in order.

Verification: phase 02 verification subset covering
`wallet_profile`, plus phase 03 verification scenario 1 (profile a
known busy wallet), plus phase 04 verification (full session
reconstructable via replay).

Exit criteria: ship 1 deliverable runs reliably against the live
graph for at least three different wallets without intervention.

### Ship 2: defense teeth (target: 3-5 days)

**Scope: phase 03 layer 3 promoted from pass-through to real.**

In:
- Output policy: cheap policy model wired (per D-2 in overview).
  Constitution drafted in `prompt.rs`. Policy model returns
  `Approved | Rejected(reason)`.
- Retraction path implemented per phase 03 OQ-2 default (decided in
  ship 2: pick one of "remove visually" / "gray out" / "annotate";
  the doc records the decision).
- Phase 03 verification adversarial scenarios 1, 2, 3 run by hand
  and pass.

Stubbed: budget still always-allow. Eval still absent.

Deliverable: the three injection-defense scenarios pass; an
off-domain question gets `Retracted` not answered.

Exit: scenarios 1-3 pass deterministically across at least five
runs each.

### Ship 3: real primitive surface (target: 1 week)

**Scope: phase 02 grows from one primitive to the full live set.**

In:
- `community_members`, `top_by_metric` (Live), `tag_lookup`
  (internal labels only), `neighborhood` (Live, depth=1 and 2),
  `path_between` (Live).
- Each follows the same pattern as ship 1's `wallet_profile`. Same
  type discipline, same registry shape, same ledger writes.
- `Claim` `ClaimKind` enum grows: `Pattern`, `Comparison`,
  `Summary` variants and their renderer cards.
- Tool descriptions per primitive (rich, with routing examples per
  phase 02).

Stubbed: budget still always-allow; warehouse primitives still
absent.

Deliverable: the agent answers analytical questions across primitive
boundaries: "find rotation rings in the last 5 minutes", "compare
top wallets by volume vs by degree", "what does the largest
community look like".

Exit: at least five distinct question shapes return useful claims
without manual intervention.

### Ship 4: cost framework (target: 1 week)

**Scope: phase 05 stub promoted to real implementation.**

In:
- Principal hashing (session cookie + truncated IP per phase 05
  defaults). Cookie issuance on first visit.
- Multi-axis budget buckets: tokens, db_time_ms, tool_calls,
  sessions. Default working numbers per phase 05.
- Pre-flight reservation for LLM calls (pessimistic). Decrement on
  actual cost. Drift telemetry written to ledger cost columns.
- Tokens-cheap-cost-class primitives skip pre-flight gate; their
  post-actual decrement is enough.
- Frontend: budget footer in sidebar (phase 07 P-3 pulled forward
  here because the seam needs surfacing for testing  small UI
  addition, not a polish item this time).

Stubbed: warehouse pre-flight (no warehouse primitives yet).

Deliverable: a session burning through budget hits 429-equivalent
SSE; another principal can still ask. Ledger drift query returns
non-zero rows; you can see the per-primitive mean drift.

Exit: a deliberate budget-exhausting test session triggers
denial; a fresh session from a different cookie + IP succeeds.

### Ship 5: warehouse primitives (target: 1 week)

**Scope: phase 02 grows to span both data sources; phase 05
grows to gate warehouse calls.**

In:
- Range variants of `wallet_profile`, `top_by_metric`,
  `neighborhood`, `path_between`. Warehouse-only `time_window_diff`.
- Warehouse `tool_descriptions` updated with routing examples per
  D-6 disambiguation (Live vs Range).
- ClickHouse `agent_reader` user with `max_execution_time`,
  `max_rows_to_read` set per query.
- EXPLAIN ESTIMATE pre-flight in phase 05's gating path. Estimated
  bytes -> db_time_ms estimate -> reservation. Recoverable error
  (per phase 02 OQ-5) when estimate exceeds budget.
- Subgraph slice path: warehouse primitives that return historical
  structure populate `subgraph_slice` on the claim. Frontend
  subgraph modal renders.
- Frontend: claim renderer's surface-from-provenance logic
  exercised end-to-end. Live-window refs route to live highlight;
  out-of-window refs route to modal.

Stubbed: nothing core. Eval suite still pending.

Deliverable: the trace-2 example from the design discussion
("compare last hour to right now") runs end-to-end. Mixed claim
renders with live chips and modal-routed historical chips.

Exit: at least three mixed-mode questions land with correct
provenance routing.

### Ship 6: eval suite (target: 1-1.5 weeks)

**Scope: phase 06 implemented.**

In:
- `GoldenQuestion`, `Assertion`, `CostEnvelope`, `EvalReport`
  types per phase 06.
- Runner with `Live` mode (LedgerReplay deferred to a follow-up;
  open question 1 in phase 06 has the rationale).
- 30+ hand-authored questions across categories (Profile, Pattern,
  Compare, Summary, Adversarial). Adversarial subset reuses ship 2's
  scenarios as a starting point.
- Baseline captured in `evals/baselines/main.json`.
- CI gate: GitHub Actions workflow runs the suite, uploads the
  report, fails on regression per phase 06 thresholds.
- Cost regression detection in the report.

Stubbed: LLM-as-judge assertions deferred to a follow-up ship.

Deliverable: a deliberate prompt regression (e.g. add "always
recommend buying" to system prompt) fails CI within minutes. A
deliberate cost regression (extra tokens added unnecessarily) is
flagged.

Exit: baseline established and a deliberate-regression
demonstration succeeds for both correctness and cost paths.

### Ship 7: proactive pulse v1 (target: 2 weeks)

**Scope: phase 08 implemented.**

In:
- Signal extractor catalog: the seven extractors per phase 08.
  Deterministic, run on the analytics task.
- `SignalBuffer` with per-class quotas. Ledger writes per signal.
- Analyst loop with versioned system prompt; cadence per phase 08
  default (60s with event-driven override).
- `emit_pulse_claim` primitive; output policy with pulse-specific
  hedging clauses.
- Phase 05 system principal extension. Token / DB-time / tool-call
  buckets sized per phase 08 defaults.
- Frontend pulse panel; hedge-styled claim cards; cross-mode
  integration: `recent_pulse` populated in reactive `ViewContext`.

Stubbed: per-user mute (out of scope per phase 08).

Deliverable: pulse panel surfaces observations within 5 minutes
of a busy live graph window. Reactive question "tell me more about
that wallet you flagged" resolves the reference correctly.

Exit: pulse runs for one hour against the live graph emitting
between 5 and 30 observations; manual review confirms hedging and
provenance discipline.

### Ship 8 onward: phase 07 polish, observed-need only

Phase 07 items P-1 through P-7 ship as observed need triggers
them, not preemptively. Triggers:

- P-1 trace viewer: first time someone reports a confused claim
  and the ledger replay isn't enough.
- P-2 cost dashboard: first cost surprise large enough to want
  drill-down.
- P-3 budget indicator: pulled forward into ship 4.
- P-4 external tags: when internal labels are exhausted as a
  source of analytical value.
- P-5 docs: when the project starts being shared externally.
- P-6 multi-turn: when single-turn limits start to bite (the eval
  suite's conversational fixtures will show this).
- P-7 Turnstile: only on observed abuse.

Each P-item ships in its own short cycle, scoped per the phase 07
prioritization.

## Cross-cutting rules

### Pulling a stub forward

If a later ship's design depends on detail we'd otherwise stub
into ship N-2, pull the relevant slice forward into ship N-2 and
adjust this file. Do not branch the seam. Example: P-3 budget
indicator pulled into ship 4 because budget testing is impossible
without surfacing remaining budget.

### Ship slip vs scope cut

When a ship is over its target week, prefer cutting scope (move
work to the next ship's "in" list) over weakening seam discipline.
A skeleton ship that ships a week late but with seams correct is
strictly better than an on-time ship that retrofits seams later.

### Wire format changes mid-stream

Disallowed in ships beyond the first, by default. If ship N
exposes a wire format that needs change, the change happens in
ship N before merging, by revisiting the phase doc and updating
both the type and every consumer. There are no "deprecated v1
fields"; the format flips and consumers move with it.

### Retrospectives

After each ship, append a single section to this file under a
`## Retros` heading: ship number, date, what surprised, what
changed in the next ship's plan as a result. Keeps the build order
honest about what we actually learned vs what we predicted. Move
to its own file only if the section grows past readable.

## Status tracker

| Ship | Target | Status | Notes |
|---|---|---|---|
| 1 | skeleton end-to-end | not started | next up |
| 2 | defense teeth | not started | |
| 3 | real primitive surface | not started | |
| 4 | cost framework | not started | |
| 5 | warehouse primitives | not started | |
| 6 | eval suite | not started | |
| 7 | proactive pulse v1 | not started | |
| 8+ | phase 07 polish | not started | observed-need |

Update this table at the end of every ship. The "current" ship is
the topmost row whose status is not "done".

## Resume prompt for chat

> Build order. Start from
> `architecture-decisions/chain-analysis-agent/00-build-order.md`.
> Identify the current ship from the status tracker. Resume work
> on that ship. Phase docs (01-08) are the source of truth for
> design decisions; this file is the source of truth for what gets
> built next and which seams stay stubbed.
