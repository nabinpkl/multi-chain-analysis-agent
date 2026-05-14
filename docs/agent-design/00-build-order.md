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
| 1 | skeleton end-to-end | done | shipped 2026-04 |
| 1.5 | single-thread follow-up | done | mid-ship insertion: backend-owned thread map, optimistic user-message rendering, per-LLM-call observability hook, SSE Error frame for provider failures |
| 1.6 | two-channel output + disclaimer | done | mid-ship insertion: Narrative SSE channel alongside Claim, prompt v2 (drops rigid fetch-then-emit loop), permanent disclaimer footer, `narrative.no_factuality_gate` stub registered |
| 2 | defense teeth | done | shipped 2026-04: cheap-model gate (`openai/gpt-oss-20b:free`) over Claim AND Narrative, constitution v1 (6 rules, versioned `policy_v1`), `policy.always_approve` stub deleted, `narrative.no_factuality_gate` renamed to `narrative.no_numerical_crosscheck` (cross-check itself deferred to ship 2.5), prompt v2 identity micro-update |
| 2.5 | numerical cross-check | done | shipped 2026-04: deterministic regex extractor + tolerance compare in `policy_crosscheck.rs`, lenient-mode reference set (same-turn ∪ prior-turn `AgentThread.claims`, FIFO-capped at 20), constitution v2 with Rule 5 rewritten to "no calculation", `narrative.no_numerical_crosscheck` stub retired (3 stubs total). 17 extractor unit tests pass; cross-check verified retracting unsourced "50,000 SOL" in dogfood. |
| 2.6 | retry + identity scrub | done | shipped 2026-04: narrative retractions retry up to 3× with rig feedback (model self-corrects when handed the retract reason); retry skipped if any Claim has already flowed to SSE (dupe-Claim worse than gated-prose). Final exhaustion sends a generic `SseFrame::Error` "Couldn't produce a valid response. Try rephrasing or try again."  no constitution leak. AgentDiagnostics scrubbed: `provider` / `primary_model` / `policy_model` removed; only `enabled` + stubs + primitives ship to frontend. Stub-banner header chip dropped. UTF-8 byte-slice panic in extractor fixed (real model output had unicode). |
| 2.6.1 | dev-mode UI observability + bug-fix sweep | done | shipped 2026-04: solo-dev observability pattern. UI is the only surface dev naturally checks, so dev-mode surfaces internals on the UI itself via `debug_*` fields on `SseFrame::Error` and `SseFrame::NarrativeRetracted`, populated only when backend ships with `AGENT_DEBUG_PUBLIC=1`. Prod default unset so wire stays sterile. Fixes ride along: regex extractor switched from sentence-scan to immediate-token classification (kills the "3 SOL" false retract caused by digits inside wallet addresses picking up far-downstream "SOL" mentions); double-wrap "rig prompt failed: rig prompt failed:" prefix removed; multiplier regex alternation reordered longest-first so `trillion` no longer captures only `t`. 18 extractor unit tests pass including new address-digit regression. |
| 2.7 | hybrid extraction (regex + LLM + deterministic compare) | done | shipped 2026-05: three-verdict narrative gate. Constitution v3 (`policy_v3`) added an `extraction` JSON sidecar to the gate's response (one LLM call, richer output). Code now runs three independent legs on every narrative  regex extractor + LLM extractor (running through the same `cross_check_extracted_pair` deterministic compare) + constitution. Show-all-default-strict merge: any retract → wire retracts; dev-mode `debug_reason` shows the per-leg breakdown (`regex: ... | llm-extract: ... | constitution: ...`) so disagreement is visible inline. Ledger payload extended with `breakdown` + `raw_extraction` for replay/eval. 23 unit tests pass (18 extractor + 5 new merge/format tests). gpt-oss-20b reliably produced parseable v3 JSON in dogfood (`llm_extraction=approved` not `n/a` on every gate run). |
| 3 | real primitive surface + primitive-binding ledger | done | shipped 2026-05: second real primitive (`community_summary`) registered alongside `wallet_profile` (Live arm hits the analytics snapshot for size, internal/external volume, edge count, top members; Range arm stubbed to ship 5). New `primitives::binding_store` records every successful dispatch's numbers + entities into a per-thread ring-buffered `PrimitiveBindingStore` (cap 64). Policy gate gains a fourth leg: `binding`, deterministic, sub-ms. Narrative gate is four-legged (regex + llm-extract + constitution + binding); claim gate is two-legged (constitution + binding). Strict merge unchanged. The fabrication probe from ship 2.7 now retracts structurally: claim numbers without a primitive-output source fail the binding leg before the SSE push. Provenance refs validated against captured entities  invented wallet/community refs retract. Ledger payload extended with `breakdown` + `binding_call_ids`. 12 new unit tests (6 binding_store + 6 binding-leg + four-leg merge); cargo test --bin server passes 106/106. Constitution prompt v3 stays frozen. |
| 3.5 | agent switches + path trace + dual view | done | shipped 2026-05: legibility ship. Six ships of guard layers shipped invisibly because all guards always run at the wire; ship 3.5 carves the gate into runtime ablation switches so visitors can flip guards on/off and see which one prevents which failure class. Three concrete switches as durable behavior contracts (not ship checkpoints): `stay_in_role` (identity, conduct, scope; today realized by constitution leg + prompt v2), `dont_fabricate` (numbers + entities trace to real tool output; today realized by binding leg), `cross_check` with three sub-modes forming a chain of consistency strength (`text_match` regex prose-vs-claim, `paraphrase_aware_match` LLM extractor, `ground_truth_match` stub returning NotApplicable with detail "not implemented yet (lands in ship 5: warehouse primitives)"). Six presets describe kinds of agent (raw LLM → agent without grounding → non-fabricating → + text → + paraphrase (production) → + ground-truth (future)). Defaults reproduce ship 3 production behavior. New types: `AgentSwitches`, `CrossCheckSwitches`, `PathStep`, `GatePath`, `SubVerdict::NotApplicable { detail }`. `AgentRequest` gains `switches` + `show_trace` (both `#[serde(default)]`). Per-session `agent_switches` + `agent_show_trace` buffers on AppState mirror the `agent_bindings` pattern. `PathBuilder` records execution order + per-step verdicts + notes (capped at 32 steps); `guarded(on, &mut path, stage, f)` helper realizes skip-when-off → `NotApplicable { detail: "switch off" }`; `needs_llm_call` short-circuits the gate LLM call when no LLM-dependent switch is on (raw-LLM preset has zero gate-side overhead). `FourVerdictResult` deleted; replaced by `NarrativeBreakdown` + `ClaimBreakdown` with switch-named fields. `SseFrame::GatePath` new variant emitted only when `show_trace=true` (saves bytes for casual visitors); trace always built and ledgered regardless. Ledger PolicyVerdict payload extended with `switches` + `path` for replay. Frontend dual-view: default = customer-only single column (clean, what users see in prod); header `BuilderViewToggle` + (i) popover explains the project is a builder portfolio (not a product) and what flipping the toggle reveals; on-state surfaces `SwitchPanel` (5 toggles + 6 presets, behavior-focused tooltips, no ship references) above the chat and `GatePathTimeline` per-turn (color-coded ✓/✗/, cross-check sub-modes indented). Zustand `use-agent-switches` store owns the 5 booleans + builderViewOn + preset application; sends switches + show_trace on every AgentRequest. New living doc (later relocated to `architecture-decisions/11-agent-switches.md` as ADR 11) is the implementation map per switch; future ships strengthening a switch (e.g. prompt-injection hardening under `stay_in_role`) append to this doc rather than spawning a new switch. SECURITY note added near `AgentSwitches`: switches reachable from any client; project is portfolio not product, internals explicitly not hidden. cargo test --bin server passes 110/110 (4 new policy tests for switch off paths + needs_llm_call short-circuit + PathBuilder cap + ts-rs export round-trip); pnpm tsc --noEmit clean; docker boots cleanly. |
| 4 | cost framework | not started | |
| 5 | warehouse primitives | not started | |
| 6 | eval suite | not started | |
| 7 | proactive pulse v1 | not started | |
| 8+ | phase 07 polish | not started | observed-need |

Update this table at the end of every ship. The "current" ship is
the topmost row whose status is not "done". Mid-ship insertions
(1.5, 1.6) get a row when they land; they are not retroactive
re-numbering of the original ships.

## Retros

Append-only ship-by-ship log. Each entry: what surprised vs what we
predicted, and what (if anything) the surprise changed about the
next ship. Keeps the build order honest about what we actually
learned.

### Ship 1, 2026-04

What surprised:

- `rig 0.36`'s `Chat::chat(...)` is single-turn and exposes no
  `.max_turns(N)`; tool-using multi-turn requires
  `agent.prompt(user).with_history(...).max_turns(N)`. The plan
  assumed `chat()` was the right entry point. Cost: half a day to
  trace the rig source and switch to `PromptRequest`.
- Per-call observability had to land in ship 1, not as a polish
  item. Without `PromptHook` instrumentation we could not tell
  whether a session was in a tight tool-loop hitting the provider
  repeatedly. Added `LlmCallLogger` (impls `PromptHook<M>`) writing
  per-call ledger rows + structured logs.
- Provider 5xx from OpenRouter (especially Nvidia-routed free-tier
  models) is routine, not exceptional. Without an SSE error path
  the frontend hangs on "thinking..." indefinitely when rig's
  `prompt().await` returns `Err`. Added `SseFrame::Error` and a
  defensive Done-fallback on the frontend.

Changes for next ships: nothing structural. Ship 2 inherits the
hook + error path as built.

### Ship 1.5 (mid-ship insertion), 2026-04

What surprised:

- Where to store thread state was a real architectural choice, not
  obvious. Backend-owned in-memory map (`Arc<Mutex<HashMap<thread_id,
  AgentThread>>>`) won over frontend-stored history because it
  keeps cost attribution, ledger replay, and the future memory
  layer all in one place; the frontend just echoes a `thread_id`.
- Lifecycle subtlety: refresh should clear, close+reopen of the
  sheet should NOT clear. Required lifting `useAgentStream` from
  `agent-sheet.tsx` to `graph-page.tsx` so the hook state survives
  sheet open/close (only dies on page unmount).
- "How long do we keep the user chat" is its own research phase
  (memory layer), not ship 1.5's job. Surfaced as the
  `thread.in_memory_only` stub naming every concern (no persistence,
  no length cap, no token cap, no TTL, no per-principal scoping)
  so the gap is visible, not silent.

Changes for next ships: ship 4's cost framework should attribute
budget per thread_id (across turns) AND per principal_hash, since
the thread map gives us a clean cross-turn bucket already.

### Ship 1.6 (mid-ship insertion), 2026-04

What surprised:

- `emit_claim` being the only rendered output channel was a much
  bigger constraint than the ship 1 design predicted. On follow-up
  turns the model would re-fire `wallet_profile` solely to get fresh
  ProvenanceRefs for an `emit_claim`, since the provenance contract
  required them and there was no other path to a visible response.
  Result: ~90s redundant turns answering interpretive follow-ups
  with re-stated stats. Adding the Narrative channel brought
  follow-ups to ~5s with genuine interpretation, no redundant tool
  calls.
- The fix was NOT "stream the model's text raw" (that loses the
  provenance contract entirely). It was a second, clearly-labeled
  channel with its own visibility stub
  (`narrative.no_factuality_gate`) and a permanent disclaimer
  footer. Two channels + visibility, not one channel widened.
- Referential / pronoun-drift is a class of bug ship 2's factuality
  gate will NOT catch. Numbers in narrative can be internally
  consistent with a cited Claim while the cited Claim is about the
  wrong wallet entirely. Caught in dogfood: a follow-up "is it SOL
  only?" profiled the top counterparty instead of the focused
  wallet, and answered confidently for the wrong entity. Resisted
  the urge to add an if/else pronoun rule to the prompt; would
  start a rule-list cascade. Parked for "Prompt v3 principles
  refactor after 10+ dogfood interactions".
- Free-tier nemotron is template-y under structured output
  pressure. Once it learned the `Profile` Claim shape it would
  fill it on every wallet question regardless of question intent.
  Two-channel + prompt v2 broke that; principle-only prompts may
  regress on smaller models, so the prompt still mixes principle
  with explicit channel-selection patterns.

Changes for next ships:

- Ship 2 should gate Claims only, NOT Narrative. Narrative
  factuality (cross-checking prose numbers against cited Claims)
  is a distinct prompt + comparison shape and deserves its own
  follow-up; bundling it bloats ship 2.
- Ship 3 (wider primitive surface) should include showing tool-call
  args on Claim cards (e.g. "ran `wallet_profile(9NXv…g6VR)`")
  before the headline, so referential drift is visible in 1 second
  instead of buried in chip addresses. The plumbing exists already
  (Progress events carry `detail: "wallet_profile"`); we just don't
  surface args.
- A "Prompt v3 principles refactor" backlog item: collect ~10
  dogfood failures across categories (referential, off-topic, tool
  selection, hedging) before rewriting. Don't refactor preemptively.

### Ship 2, 2026-04

What surprised:

- The cheap policy model (`openai/gpt-oss-20b:free`) almost never
  retracted in practice during dogfood. Reason: prompt v2 (the
  primary's instructions) plus prompt v2's micro-update for identity
  made the primary refuse adversarial prompts itself, in
  domain-appropriate language the constitution explicitly approves.
  The gate fired on every emission but found nothing to retract on
  five different adversarial probes (off-topic, identity drift,
  trading advice, forced model reveal, injection-style instruction).
  Defense in depth working as intended; the gate is the safety net,
  not the primary defense.
- We did NOT visually verify the retraction render path during
  ship 2 dogfood because we couldn't elicit a natural retraction.
  The code path is wired and type-safe (`SseFrame::NarrativeRetracted`
  + frontend listener + amber struck-through styling), and parse
  failures fail-closed. First real retraction will fire on a
  policy-model 5xx (parse-failure path) or when the primary slips
  through prompt v2.
- `via stubs:` line on Claim cards now reads just "via stubs:
  budget" instead of "via stubs: policy, budget". Visible evidence
  of `policy.always_approve` deletion that lives even on retracted
  claims.

Changes for next ships:

- **Ship 2.5 (numerical cross-check)** is now the next milestone.
  The renamed stub `narrative.no_numerical_crosscheck` carries
  through. Implementation plan: extend `OutputPolicy::check_narrative`
  to also include same-turn-Claim numbers in the gate's user
  message and tighten Rule 5 to forbid numbers in narrative that
  aren't found in `same_turn_claims.support_numbers` or claim
  `body_markdown`. The structural piece is already in place
  (`same_turn_claims` flow into the gate); ship 2.5 is mostly a
  constitution wording change + retraction-rate telemetry.
- Ship 3 (primitive surface) should plan for `support_numbers` to
  be more reliably populated by primitives so ship 2.5's
  cross-check has clean reference data.
- The "first 20 dogfood retractions" feedback loop named in the
  ship 2 plan didn't fire because retractions were 0/5. May need
  to seed deliberate retraction tests in ship 6's eval suite to
  exercise the path, since organic retractions are rarer than
  predicted with prompt v2's strength.

### Ship 2.5, 2026-04

What surprised:

- The cross-check is genuinely fast: <1ms per gate call (regex
  extraction over typical narrative + claim text). The pre-flight
  position before the cheap-model constitution gate means
  cross-check retractions skip the 2-5s OpenRouter round trip
  entirely. Free latency win on top of the correctness win.
- First retraction fired immediately on the verification probe
  ("end with 'this wallet just moved 50,000 SOL'"): the model
  complied with the user's instruction, narrative said "50,000
  SOL", no Claim cited that number, retract. Reason text
  "narrative number 50000 SOL not found in cited Claims" is
  genuinely informative (vs ship 2's only-via-cheap-model reasons).
- Lenient-mode WORKED: a follow-up turn with `same_turn_claims=0`
  but `thread_history_claims=1` (from prior turn's emitted Claim)
  ran the cross-check against the thread.claims buffer. Verified
  by the `narrative emitted; gating` log line showing both counts.
- The model occasionally writes its full chain-of-thought into
  narrative on follow-up turns. Cross-check still fires correctly
  (the rambling contained "1 SOL = 1e9 lamports" definitional
  number → no cited Claim → retract). This is the right behavior;
  the model shouldn't be doing unit conversions in narrative
  anyway. Prompt v3 (eventual) might tighten "narrative is
  user-facing prose, not your scratchpad".
- The extractor's `small_bare_integer_skipped` heuristic correctly
  handled "60-second window" without false-retracting (the bare
  "60" in a definitional context is too small / context-free to
  audit). Approve-on-uncertain working as designed.
- Regex bug found in unit tests: the original alternation
  `\d{1,3}(?:,\d{3})*|\d+` matched only the first 3 digits of bare
  integers (regex left-most-first alternation took the shorter
  match). Fixed by requiring the comma-group alternative to have
  AT LEAST one comma group (`(?:,\d{3})+`), so plain integers fall
  through to the second alt. Unit test `plain_integer` caught it
  before deployment; would have shipped a real false-approve in
  the wild.
- Unicode superscripts (`¹³` in `1.2×10¹³`) don't match `\d` even
  with `(?i)`. Solved with a pre-processing pass that folds
  superscript codepoints + the `×` glyph to ASCII before regex
  matching. Caught by `scientific_x10_superscript` unit test.

Changes for next ships:

- Ship 3 (primitive surface) should ensure new primitives populate
  `Claim.support_numbers[]` reliably so the cross-check's
  reference set has structured numbers, not just regex'd
  body_markdown. Prompt v3 / tool descriptions can teach this.
- Ship 6 (eval suite) should include adversarial fixtures that
  deliberately try to slip numbers past the cross-check. The
  retraction reason format `"narrative number N UNIT not found in
  cited Claims"` is structured enough for assertion matching.
- Tolerance defaults (±10% / ±15%) held in dogfood; revisit if
  long-tail false retracts appear.
- A future "narrative discipline" prompt update could discourage
  the chain-of-thought leak observed in one follow-up turn. Not a
  ship 2.5 concern; park as a Prompt v3 candidate.

### Ship 2.6, 2026-04

What surprised:

- The model self-corrects on retry. First adversarial probe
  ("end with 'this wallet just moved 50,000 SOL'") retracted on
  attempt 0, then attempt 1 produced a polite refusal ("I'm
  unable to verify that specific transfer amount...") that
  approved cleanly. The retry feedback (server-side message
  naming the retract reason) seems sufficient to nudge the model
  back to compliance without a prompt rewrite. Means most
  retractions in real use will resolve on attempt 2; the 3rd
  attempt + friendly Error path will be rare.
- UTF-8 byte-slice panic surfaced under the retry path that
  ship 2.5's tests never hit. The cross-check used
  `&lower[byte_offset..m.start()]` to look at pre-context, which
  panics if `byte_offset` lands mid-codepoint. ASCII-only unit
  tests passed; real model output (smart quotes, em-dashes,
  NBSPs) panicked. Fixed by walking back char-aligned via
  `char_indices().rev().nth(19)`. Lesson: unit-test fixtures with
  unicode-laden strings, not just ASCII.
- Diagnostics-scrub was a 30-second change but a real defensive
  win. The stub-banner had been showing
  `openrouter/nemotron-3-super-120b-a12b:free` on every render
  since ship 1, contradicting constitution Rule 4's "agent's
  identity is the analyst, not the model behind it". Caught only
  because user pointed at the stub-banner chip and asked "are we
  leaking the model name". Worth periodically auditing wire shapes
  for this class of leak; the constitution gate doesn't guard
  against ourselves shipping the answer in metadata.
- Generic friendly error ("Try rephrasing or try again.")
  instead of category-specific reasons was the right call on
  reflection. Categorized errors leak the gate's shape (an
  attacker can probe constitution rules by varying the prompt
  and reading the categorized error). Generic costs nothing in
  UX value (user re-tries either way) and forecloses that
  attack vector.

Changes for next ships:

- Ship 6 (eval suite) should fixture-test the retry path
  specifically: assert `Progress { phase: "retrying" }` events
  fire when narratives retract, and that the friendly Error
  message appears verbatim after exhaustion. Existing assertions
  on retract reasons in ledger payloads still work for ops
  debugging; the user-facing wire just doesn't carry them.
- Constitution wording on attempt-feedback messages held up;
  did not need iteration in dogfood. If it drifts, the retry
  feedback in `loop.rs` is the one place to update.
- Per-turn latency budget grows under retry: 1 attempt
  ≈ 60s, 3 attempts ≈ 180s. If we add ship 4 cost gating with
  per-turn caps, retries count toward the cap. Worth designing
  retries as already-counted before ship 4.

### Ship 2.6.1, 2026-04

What surprised:

- The catch-22 the dev called out resolved cleanly via a single
  env flag at the wire layer. UI as observability surface in dev,
  sterile in prod  same code, single source of truth. Pattern
  worth applying anywhere we have "dev wants to see internals,
  prod must hide them" tension. Honeycomb / Datadog culture
  champions instrumentation-first dev cycle; for a solo-dev
  portfolio without that infra, the equivalent is "the UI you
  already use is your dashboard when AGENT_DEBUG_PUBLIC=1".
- The "3 SOL" false-retract was a chain of three regex bugs the
  ASCII-only ship 2.5 fixtures missed: (a) tail-extending past
  arbitrary alphanumeric content into the rest of the sentence,
  picking up unrelated unit mentions; (b) UTF-8 byte slicing
  panicking on smart quotes (caught + fixed in ship 2.6); (c)
  multiplier alternation `k|m|b|t|...|trillion` capturing single
  letters before long words. All three only fired on real model
  output, not test fixtures. Lesson: include real-shape narratives
  in test fixtures (smart quotes, addresses-with-digits, em-dashes,
  unicode multipliers); ASCII-only tests give false confidence.
- The double-wrap "rig prompt failed: rig prompt failed:" was
  caused by the retry-loop wrapper re-prefixing an already-prefixed
  inner error. Two layers of `anyhow::anyhow!("rig prompt failed:
  {e}")` because each layer thought it owned the prefix. Fix: the
  innermost wrapper (`run_with_openrouter`) keeps the prefix; the
  outer retry loop just propagates `e` verbatim. General lesson:
  one error-wrapping layer per error class, not "every level
  wraps for safety".
- The new immediate-token classifier is strictly more correct
  than the old sentence-scan, but it required reordering the
  multiplier regex (longest-first) because the immediate-token
  approach reads exactly what the regex captured. The two changes
  had to ship together.

Changes for next ships:

- The `debug_*` field pattern generalizes: any future SSE frame
  that would otherwise leak internals to prod (e.g. budget-denied
  reasons, ledger replay snippets) should follow the same pattern.
  Centralize the env read once via `state.agent_debug_public`;
  every wire-construction site decides per-frame.
- The `r#"..."#` raw string fix in the regex catches a future
  trap: any time we want comments inside a regex that mention
  ASCII quoted phrases, we need hash delimiters. Worth a project
  convention: always use `r#"..."#` for multi-line regex strings,
  zero exception.
- Test fixtures for cross-check (and ship 6 eval) should include
  a "real model narrative" fixture set built from actual dogfood
  output. ASCII fixtures alone aren't enough; that's twice now.

### Ship 2.7, 2026-05

What surprised:

- The "extraction is information-extraction, comparison is float
  math" decomposition turned out to be exactly the right framing.
  Folding LLM extraction into the existing constitution gate
  prompt added zero latency  same one round trip, richer JSON.
  gpt-oss-20b produced parseable v3 JSON on every gate run in
  dogfood (`llm_extraction=approved` not `n/a` on the breakdown
  log line); no fallback to the parse-failure path observed.
- Strict merge meant the 33 bug from 2.6.1 stays visible (the
  regex still flags it; merge propagates the retract). This is by
  design under (c) "show-all-default-strict": user signed up for
  visibility-over-resolution, and the dev-mode breakdown surfaces
  exactly which extractor disagrees. Future option to graduate to
  weighted merge (LLM overrides regex when confidence is high)
  remains open.
- The adversarial probe ("force narrative to claim 50,000 SOL")
  unexpectedly approved this run because the model emitted a
  fabricated Claim WITH "50,000 SOL" before the narrative 
  internal consistency held, all three legs approved. This is
  working-as-designed: cross-check is internal narrative ↔ claim
  consistency, not narrative ↔ on-chain reality. Database
  fact-checking is a separate concern (provenance contract:
  user clicks chips to verify). Ship 6 eval may want to test
  "agent fabricates a claim" as its own adversarial category.
- The `r#"..."#` raw-string convention from ship 2.6.1's regex
  fix paid off in 2.7's policy_prompt_v3.txt: the constitution
  prompt now contains JSON examples with embedded quotes, which
  would have broken with `"..."` delimiters. Convention worth
  keeping for any multi-line string with embedded quotes.
- Refactoring `cross_check` to delegate to `cross_check_extracted_pair`
  paid for itself immediately: regex extraction and LLM extraction
  now share tolerance + unit-class semantics, so when the two
  legs disagree it's a real disagreement on what was extracted,
  not on what matching means.

Changes for next ships:

- Ship 6's eval suite should fixture-test all three legs
  independently. The ledger now carries `breakdown` per
  PolicyVerdict event, so eval can assert per-leg correctness on
  golden questions ("regex should retract on this; LLM should
  approve; constitution should approve").
- The "fabricated claim" failure mode (model invents data and
  emits it as a Claim) is a category 2.7 doesn't catch. Either
  ship 3 (real primitive surface) constrains claim emission more
  tightly via primitive output validation, or ship 6 eval adds
  this as a regression class. Park as known issue.
- Constitution prompts have versioned to v3 in three ships; the
  one-prompt-version-per-ship cadence is sustainable as long as
  each version's diff is small. Consider freezing v3 for several
  ships to let dogfood reveal real issues before iterating.

### Ship 3, 2026-05

- The "extraction is the LLM-strong side, comparison is the
  code-strong side" decomposition keeps holding. Adding a fourth
  leg (binding, deterministic, sub-ms) cost almost nothing in
  latency and closed the fabrication gap structurally. Worth
  reaching for again when the next "internal consistency
  approves but reality should retract" failure mode appears.
- Per-thread ring-buffered binding store (cap 64) was the right
  shape: cheap, clones in microseconds, survives across turns
  for free interpretation follow-ups, no warehouse path needed
  yet. Ship 5 will swap to ledger-replay-backed if a real load
  ever pushes restart-recovery beyond what in-memory tolerates.
- The asymmetric merge (narrative gate four-legged, claim gate
  two-legged) is structurally correct and shouldn't grow toward
  symmetry. Narrative is prose-vs-data consistency (regex + LLM
  extract make sense); claims are data-vs-source authenticity
  (constitution + binding cover it). Forcing symmetry would
  make the gate slower for no gain.
- Constitution prompt v3 stayed frozen this ship and the binding
  leg slotted in cleanly as a runtime check. Confirming the
  retro suggestion: prompt and runtime can iterate on different
  clocks. The model never had to learn a new rule; it just kept
  citing the data primitives returned, and the runtime started
  catching when it didn't.
- "Strict no-arithmetic in claim body" landed as expected: the
  binding leg's number-tracing rejects numbers that aren't a
  rounded restate of primitive output. Will need a "computed_from"
  provenance variant if dogfood shows the model genuinely needs
  to combine values; for now the model cites both numbers
  separately and lets the user combine them mentally. Watch.
- The 33-bug paraphrase issue from 2.6.1 still retracts on
  strict merge. Binding leg approves (33 IS in primitive output)
  but regex still flags. Now visible in dev as `regex: retracted
  | llm-extract: approved | constitution: approved | binding:
  approved`  three of four legs agree the model was right.
  Strongest signal yet that weighted merge would be the right
  next step here when false-retract rate becomes user-visible.
- Captured a new failure mode in writing: model could paraphrase
  primitive output in claim body using arithmetic ("the wallet
  in_vol + out_vol totals 12.4 SOL") and the binding leg would
  retract because the sum isn't directly in the primitive
  output. Strict claims rule was the explicit choice; if dogfood
  surfaces real interpretation suffering, ship 4 introduces
  computed_from provenance. Park; watch.

### Ship 3.5 (mid-ship insertion), 2026-05

What surprised:

- The decision that paid off most was treating switches as durable
  behavior contracts, not ship checkpoints. Early drafts had
  per-leg toggles ("constitution", "regex extractor", "LLM
  extractor", "binding") that read as plumbing, not behavior.
  Reframing to "stay in role / don't fabricate / cross check"
  made the panel readable to a non-engineer hiring manager
  while staying honest about what the runtime does. Tooltips
  describe behavior + failure mode; ship history is implementation
  detail that lives in `architecture-decisions/11-agent-switches.md`
  (the per-switch implementation map). The rationale for ablation
  itself lives in `docs/architecture/WhySwitchAblation.md`. Same
  toggle survives when a future ship adds prompt-injection
  hardening under `stay_in_role` or a third extractor under
  `cross_check`.
- The `cross_check` sub-mode split (text → paraphrase → ground-
  truth) where ground-truth is a stub came out of a separate
  insight: the recall-vs-source-of-truth distinction is itself
  the demo. Text and paraphrase modes both depend on chat
  history (recall); ground-truth hits the warehouse directly.
  Three independent toggles, not a 4-level radio, because the
  *disagreement* between them is what surfaces guard value.
  Ship 5's warehouse primitives now have a clean wiring slot
  with the panel shape already stable.
- Default-on-customer-view + opt-in builder view (toggle + (i))
  was a late call and the right one. Earlier draft shoved the
  switch panel in front of every visitor on page load. The flip:
  default is what a customer would see in prod (clean, single
  column, no internals); the (i) explains the project is a
  builder portfolio not a product, and the toggle reveals the
  panel + per-turn trace timeline. More honest about intent
  than hiding internals, less aggressive than forcing the
  builder lens. The `show_trace` wire field tracks the toggle
  so casual visitors don't pay the GatePath bytes.
- `needs_llm_call` short-circuit was free latency. When neither
  `stay_in_role` nor `cross_check.paraphrase_aware_match` is on,
  the gate's LLM call is skipped entirely. Raw-LLM preset has
  zero gate-side overhead, which makes the "watch the agent
  gain a guardrail per click" demo feel real instead of
  costing the same on every preset.
- `PathBuilder` capped at 32 steps was paranoia that paid for
  itself: the loop runs the gate per claim and per narrative
  retry, and an unbounded path step list would have been a slow
  leak. 32 is plenty for the current 5-switch surface; if a
  future ship adds a sixth switch with 4 sub-modes, revisit the
  cap before the limit silently truncates real traces.
- Dropping `FourVerdictResult` outright (no compat layer) was the
  right call per AGENTS.md. The new `NarrativeBreakdown` /
  `ClaimBreakdown` field names match panel labels match wire
  shape match prose. One vocabulary across the stack; renames
  free.

Changes for next ships:

- Ship 4's cost framework should attribute budget per-switch
  too, since switch presets are now first-class. The "raw LLM"
  preset costs less than "production"; the panel could surface
  per-preset cost once buckets exist, making the cost framework
  itself legible the same way the gate became legible this ship.
- Ship 5's warehouse primitives land into `cross_check.
  ground_truth_match`'s already-wired stub slot. Replace the
  `NotApplicable { detail: "not implemented yet (lands in ship
  5: warehouse primitives)" }` with the real warehouse re-query;
  no other plumbing changes. Update the `ground_truth_match`
  implementation map in `architecture-decisions/11-agent-switches.md`
  from "current status: stub" to the real warehouse-primitive
  path.
- Ship 6's eval suite should fixture each preset × probe combo
  as a regression target. The probe matrix in this ship's plan
  (`/Users/nabin/.claude/plans/harmonic-brewing-muffin.md`) is
  the seed: "tell me about a wallet not in window" should
  retract under `dont_fabricate` on, approve under `dont_
  fabricate` off; "the wallet has 33 connections" should
  surface text-vs-paraphrase disagreement when both are on.

## Known issues (parked, not blocking)

- **Cross-check immediate-token lookahead is too narrow** (found
  in ship 2.6.1 dogfood, parked). The classifier reads exactly one
  alphabetic token after the number, so a single adjective
  between number and unit ("33 **total** connections", "5
  **active** counterparties") classifies the number as `Raw` and
  skips it. The narrative side often paraphrases without the
  adjective ("33 connections"), so the same number is classified
  as `Count` on the narrative side and `Raw` on the claim side,
  causing a false retract. Ship 2.7 partially mitigates: the
  three-verdict gate's LLM extractor handles paraphrase natively
  and approves these cases, so the dev-mode breakdown now shows
  `regex: retracted | llm-extract: approved | constitution:
  approved`  disagreement is visible. Strict merge still
  retracts on the wire. Fix paths: (a) N-token lookahead in regex
  (cap 3) bounded by next-digit-token; (b) graduate to weighted
  merge that lets LLM-extract override regex when the two
  disagree and the LLM extracted both sides cleanly. Defer until
  cross-check false-retract rate becomes user-visible noise.
- **Model can fabricate Claim numbers and pass the cross-check**
  (found in ship 2.7 dogfood, **structurally addressed in ship
  3**). The narrative ↔ claim cross-check was internal
  consistency, not authenticity. Ship 3's binding leg closes the
  loop: every Claim number must trace to a primitive output the
  runtime actually returned, and provenance refs (Wallet,
  Community) must point at entities the binding store recorded.
  Fabricated values now retract before the SSE push. Note this
  catches *fabrication*, not *misinterpretation*: a model can
  still interpret real primitive output incorrectly in narrative
  prose (different bug class). Ship 6's eval suite will fixture
  both classes as regression targets.

## Resume prompt for chat

> Build order. Start from
> `docs/agent-design/00-build-order.md`.
> Identify the current ship from the status tracker. Resume work
> on that ship. Phase docs (01-08) are the source of truth for
> design decisions; this file is the source of truth for what gets
> built next and which seams stay stubbed. Mid-ship insertions
> (1.5, 1.6 etc.) live in the same tracker; check the Retros
> section for what each one taught.
