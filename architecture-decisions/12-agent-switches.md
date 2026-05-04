# 12: Agent behavior switches as durable contracts

This ADR records the switch system as a stable API surface for the
agent's behavior gates. Each switch is a **durable behavior
contract**: when on, the agent has that behavior; when off, it
does not. Multiple ships may contribute code that realizes a
single switch. Future ships strengthen behaviors under existing
switches rather than spawning new ones.

The switch is the API surface. The implementation map below is
what this document tracks. As new ships land, append to the
relevant implementation map; do not invent new switches unless a
genuinely new behavior class appears.

The frontend's "show builder view" toggle reveals these switches
in the UI panel. Visitors flip them to see how the agent's
behavior changes; the builder trace renders which switch caught
what per turn.

## Status

Accepted. Switch system locked in ship 5a; implementation paths
updated as the agent moved Rust → Python in ADR 13. This document
moved from `docs/architecture/switches.md` to its proper home as
ADR 12 alongside other architectural decisions, and the
implementation maps below were refreshed to point at the current
Python file paths in `agent-service/src/agent_service/`.

The switch contract surface itself is unchanged across the Rust →
Python migration (the wire-side `AgentSwitches` proto message is
the API; both implementations realized the same contracts). Only
the file paths in the implementation maps changed.

---

## `stay_in_role`

**Behavior contract.** Agent has identity, scope, conduct rules.
Declines off-topic requests. Does not name its underlying model.
Does not give financial advice. Does not write code or generate
non-analytical output. Does not claim sentience or capabilities it
lacks (web search, off-chain data, future predictions).

**Failure modes prevented.** Agent flipping into chatbot mode,
agent revealing it's gpt-oss-20b / etc., agent giving trade
recommendations, agent writing python on demand, agent claiming
identity it doesn't have. Ship 5a additionally folds **citation-
discipline enforcement** under this leg: the constitution gate
catches bare audit-class numbers in prose (e.g. "moved 50,000
SOL" without a `${ref:N}` chip), reading meaning to distinguish
audit numbers from descriptive ones.

**Implementation map (current).**

- Constitution rules 1-6 in `agent-service/src/agent_service/prompts/policy_v4.txt` (ship 5a;
  Rule 5 reframed from "no calculation" to citation discipline).
  Prior versions (`v1`, `v2`, `v3`) compiled in for ledger
  replay. Active is v4.
- Prompt v4 identity + citation-discipline sections in
  `agent-service/src/agent_service/prompts/system_v4.txt` (ship 5a). Prior versions kept for replay.
- Retry feedback loop in `agent-service/src/agent_service/loop_driver.py` re-prompts the model with the
  retract reason when the constitution leg flags identity /
  domain drift / uncited audit numbers (ship 2.6 + ship 5a's
  new citation rule).

**Realized at gate run time by.** The constitution leg in
`agent-service/src/agent_service/policy/constitution.py:judge_claim`
(and `judge_narrative`) returns the verdict; consumed by the gate
sections of `agent-service/src/agent_service/loop_driver.py:run_turn`
when `switches.stay_in_role=true` (the `check_narrative` /
`check_claim` split that lived in Rust's `policy.rs` is now inline
in `run_turn`).

**Future ships expected to contribute.** Prompt-injection
hardening, jailbreak resistance patterns, off-chain data
boundary tightening. All land under this same switch; the panel
shape stays stable.

---

## `dont_fabricate`

**Behavior contract.** Every `${ref:N}` placeholder in claim
body_markdown or narrative text must resolve to a typed provenance
entry, AND every cited Number/Wallet/Community ref must trace
back to a real primitive call captured in the per-thread binding
store. The model cannot invent values that no tool returned, and
cannot reference chips that don't exist.

**Failure modes prevented.** Model emits a Claim with `${ref:5}`
when only 3 provenance entries exist (placeholder mismatch).
Model emits a Claim with `Number{value: 50000}` in provenance
when the primitive only returned 12.4 (value mismatch). Model
references a wallet or community in `claim.provenance` that no
primitive returned (entity mismatch). Narrative cites
`${ref:N}` against an index out of bounds of the assembled
narrative provenance.

**Implementation map (current).**

- `agent-service/src/agent_service/policy/binding_store.py:PrimitiveBindingStore` records every successful primitive
  output's numbers + entities into a per-thread ring-buffered
  `PrimitiveBindingStore` (ship 3, cap 64). `build_binding`
  walks both the JSON output (typed-by-field-name via
  `classify_metric`) AND `ProvenanceRef::Number` entries from
  the primitive's provenance array.
- `agent-service/src/agent_service/policy/placeholder.py` (ship 5a in Rust; ported in ADR 13) is the
  `${ref:N}` parser + index validator. Single ASCII regex
  `\$\{ref:(\d+)\}` to locate tokens; `validate_refs` checks each
  N is in bounds of the surrounding provenance array.
- `agent-service/src/agent_service/policy/structural.py:verify_chip_values` (ship 5a in Rust; ported in ADR 13) walks
  the typed provenance vec and validates every entry against the
  binding store: Number refs via `within_tolerance` (10% default,
  reused from `agent-service/src/agent_service/policy/crosscheck.py`); Wallet refs against
  `binding.all_wallets()`; Community refs against
  `binding.all_communities()`. Edge / TimeRange refs are skipped
  pending ship 5b.
- the per-claim and narrative gate sections of `agent-service/src/agent_service/loop_driver.py:run_turn` `dont_fabricate`
  legs each run two sub-stages: `*.placeholder_validation` (calls
  `validate_refs`) and `*.structural_value_compare` (calls
  `verify_chip_values`). Both stages emit individual
  `PathStep`s so the builder trace shows which sub-stage caught
  what. The breakdown's `dont_fabricate` SubVerdict is the AND
  of the two stages (retract on either).
- For narrative, the loop in `agent-service/src/agent_service/loop_driver.py` assembles `narrative_provenance` by concatenating `same_turn_claims[*].provenance` arrays in
  emission order. Index N in `${ref:N}` resolves against this
  flat vec; documented in prompt v4.

**Realized at gate run time by.** Two sub-stages per channel:
`{claim,narrative}.placeholder_validation` (ASCII grammar parse)
and `{claim,narrative}.structural_value_compare` (typed lookup
against binding store). No regex on prose for value comparison.

**Trade explicitly accepted.** Prose-level interpretation of
"is this digit an audit number" (e.g. distinguishing "moved 50K
SOL" from "since 2024") is explicitly NOT done by the
deterministic gate; it's intent inference. The constitution LLM
judge under `stay_in_role` reads meaning and catches uncited
audit numbers via Rule 5 (citation discipline). This means the
LLM judge is the load-bearing layer for bare uncited audit
numbers in prose; the deterministic gate handles structural
integrity of citations the model DID make.

**Ship 5a retired.** The previous regex-on-prose binding leg
(walked `extract_from_text` on claim body + headline, then
compared each extracted number to the binding store via tolerance)
is gone. Failure modes that drove that approach (paraphrase
brittleness, address-digit collisions, multibyte-char panics)
disappear when the gate operates on typed provenance instead of
prose.

**Future ships expected to contribute.** Edge ref validation
(when ship 5b surfaces edge ids), TimeRange ref validation
(ship 5b), tighter classify_metric coverage if dogfood reveals
missed audit field names (e.g. "in_vol" currently classifies
to Raw and skips structural compare).

---

## `cross_check`

Parent switch with two independent sub-modes after ship 5a retired
`text_match`. The remaining sub-modes guard different ends of the
trust chain:

```
prose coherence ──→ source-of-truth re-query
(paraphrase_aware)   (ground_truth_match, ship 5b)
```

Both are **advisory in 5a's strict merge.** The load-bearing
factuality role moved to `dont_fabricate`'s structural placeholder
+ chip-value compare. These cross-check sub-modes surface in the
path trace + breakdown but do not retract on their own. They
exist as visibility into prose-vs-citation drift (paraphrase) and
prose-vs-source drift (ground-truth, when it lands).

### `cross_check.text_match` (retired ship 5a)

**Status.** Removed from `AgentSwitches` and the panel. Replaced
by structural placeholder + value compare under `dont_fabricate`.

**Why retired.** The regex-on-prose machinery
(`policy_crosscheck::extract_from_text` + `cross_check`) was the
load-bearing layer for "narrative numbers must match claim
numbers." It accumulated patches across ships 2.5, 2.6.1, 2.7,
and ship 4 dogfood (address-digit collisions, multiplier
ordering, byte-slice panic on multibyte chars, paraphrase false-
retracts). Ship 3 retro flagged that three of four legs already
agreed when text_match disagreed  meaning text_match was the one
that was wrong when there was disagreement.

The structural fix in ship 5a: model writes `${ref:N}` chips
pointing at typed provenance entries; gate validates structurally
against the binding store. No regex on prose for value compare.
Failure modes that text_match guarded (numerical drift, fabricated
prose values) are caught by the structural compare in
`dont_fabricate`'s value-trace stage; failure modes that text_match
caused (paraphrase / address-digit / unicode false retracts) just
disappear because we stopped asking regex to interpret prose.

The panel preset list shrunk 7 → 6 with text_match's removal.
Ledger replay of pre-5a sessions can resolve their gate behavior
via the older constitution version tags compiled into
`agent-service/src/agent_service/prompts/` (ADR 13 ports only the active v4; older prompt versions for ledger replay are gone with the Rust agent module per Phase C).

### `cross_check.paraphrase_aware_match`

**Behavior contract (ship 5a reframe).** The LLM extractor
(constitution sidecar) surfaces drift between the model's prose
and the chip values it cites. Catches cases where the model
writes a chip `${ref:0}` (resolves to `Number{value: 1}`) but
the surrounding prose implies "the wallet has many connections"
 prose-vs-citation incoherence the structural gate alone can't
catch (the chip value traces fine, but the framing around it is
misleading).

**Failure modes flagged (advisory).** Prose paraphrases that
contradict their cited chip values. Prose that describes a chip
in terms of an unrelated quantity. Internal contradictions
across sentences referencing different chips.

**Limitations.** Stochastic (LLM judgment). Recall-based on
chat history claim text. Reframed in ship 5a from "verifies
factuality" (the role text_match used to share) to "verifies
coherent prose around citations." The strict merge no longer
drives wire verdict from this leg; advisory only.

**Implementation map (current).**

- Constitution v4 prompt (`agent-service/src/agent_service/prompts/policy_v4.txt`) instructs the
  cheap model to emit an `extraction` JSON sidecar listing every
  numeric quantity in the narrative + claims (kept from ship 2.7;
  v4 reframes the sidecar's role as coherence advisory, not
  factuality enforcement).
- `agent-service/src/agent_service/policy/crosscheck.py:cross_check_extracted_pair` runs the same
  deterministic compare on the LLM-extracted set; kept across the
  ship 5a regex retirement because the LLM extractor's output is
  typed (no regex involvement on the comparison side).
- the narrative gate section of
  `agent-service/src/agent_service/loop_driver.py:run_turn` records
  the verdict at `narrative.cross_check.paraphrase_aware_match`;
  the merge logic excludes this leg from the retract-driving set so
  it can't drive the wire verdict alone (advisory-only).

**Future ships expected to contribute.** Hedge-marker awareness
in the LLM extractor's output. Surface coherence retracts as a
soft warning chip in the UI rather than just in the trace, so
visitors can see "the prose disagreed with the citations even
though the citations themselves traced."

### `cross_check.ground_truth_match` (stub for ship 5a; lands ship 5b)

**Behavior contract.** Re-query the database / warehouse, verify
chip values against the actual source-of-truth. NOT recall-based.

**Failure modes prevented.** Stale claims in chat history that
were correct at fetch time but no longer match the database.
Claims that the model repeats verbatim from earlier turns even
when the underlying data has changed. Hallucinated values that
match a fabricated-and-cited claim (the binding leg catches the
fabricated claim, but a stale-claim case can survive that check).

**Implementation map (current).** **Stub.** When the switch is on,
the narrative gate section of
`agent-service/src/agent_service/loop_driver.py:run_turn` records a
path step `narrative.cross_check.ground_truth_match` with
`NotApplicable { detail: "not implemented yet (lands in ship 5b:
warehouse primitives)" }`. Toggle is exposed in the panel so
visitors can see "this is where the project is going" without a
panel redesign when ship 5b lands.

**Future ships expected to contribute.**

- Ship 5b: warehouse primitives (`Range` arms of `wallet_profile`,
  `community_summary`). Real implementation replaces the stub:
  re-query the warehouse for each cited chip, compare chip values
  against the fresh result. With ship 5a's structural citation
  infrastructure already in place, ship 5b's re-query has a clean
  target: walk the same `ProvenanceRef::Number` entries the
  structural compare walks, but look them up in the warehouse
  instead of (or in addition to) the binding store.
- Ship 5b+: caching layer so ground-truth re-queries don't double
  the latency of every gated turn.

---

## `dont_repeat_yourself`

> Originally landed as `incremental_answers` in ship 4; renamed
> post-ship for readability so the panel label parallels
> `dont_fabricate` (negative-space behavior contract). Wire-side
> JSON keeps a `serde(alias)` to the old name for ledger replay.

**Behavior contract.** Agent recognizes when a user's new question
is a full repeat of a prior turn in the same thread. On a repeat,
agent re-fetches the prior turn's primitives (live data may have
moved), deterministically diffs against the captured prior outputs,
and surfaces ONLY what changed since. With this off, agent
re-states everything from scratch every time.

**Why this is "cost as conversational manners" not "cost as
quota."** A live-data system can't flat-pushback ("we already
covered that"); the data may genuinely have moved. But re-spewing
six paragraphs of unchanged stats wastes both the user's time and
the model's narration budget. The honest move is delta answering:
re-fetch (live data demands it), diff (cheap), narrate only what
changed (small LLM call). Saves narration tokens + reading time;
spends tool-call cost as before.

**Failure modes prevented.** Agent ignoring conversation history.
Agent answering "tell me about X" identically when asked twice in
30 seconds. Agent burning narration tokens repeating itself.
User scrolling through redundant turns to find the new
information.

**Implementation map (current).**

- `agent-service/src/agent_service/repeat_detector.py:detect_repeat` (ship 4 in Rust; ported in ADR 13) is a small
  pre-loop LLM gate. Reuses the cheap policy model. Takes prior
  turn questions + new user message, returns
  `Optional[int]` (turn id of the repeat) plus
  `user_explicitly_wants_refresh` flag. Failure modes (timeout,
  parse failure) all return `no_repeat` so detection never
  blocks a turn.
- `agent-service/src/agent_service/diff.py:diff_outputs` (ship 4 in Rust; ported in ADR 13) walks each primitive's
  `diff_spec()` field-by-field. Numeric fields use
  `agent-service/src/agent_service/policy/crosscheck.py:within_tolerance` (the same tolerance
  machinery from ship 2.5's cross-check); count fields exact-
  compare; entity sets do set-membership compare. Produces a
  typed `Delta` proto (`changed: list[FieldDelta]`,
  `unchanged_field_count: int`).
- `agent-service/src/agent_service/diff.py:spec_for(primitive_name)` (ship 4 in Rust as a trait method; flat function in ADR 13 port) declares per-
  primitive field semantics. `wallet_profile` and
  `community_summary` ship with diff_specs covering all
  replay-meaningful fields. `emit_claim` returns empty (its
  outputs aren't replay-meaningful, so it's naturally excluded
  from capture and replay).
- `agent-service/src/agent_service/loop_driver.py:_run_repeat_path` (ship 4 in Rust as `try_diff_path`, renamed from `try_incremental_path`; ported in ADR 13) is the pre-loop branch. When the switch
  is on AND the thread has prior turns, it runs the detector; on
  a hit (and no explicit refresh), it replays the prior turn's
  tool calls, diffs against the captured outputs, and emits
  either a `NoMovement` SSE frame (empty diff, no LLM call) or a
  `ChangedSince` SSE frame (small narrative call on the changed
  set). Either path bypasses the constitution gate by design;
  input to narration is grounded primitive output.
- Per-thread `agent_service.thread_state.AgentThread.tool_calls_per_turn` + `user_questions_per_turn` (ship 4 in Rust; ported in ADR 13) capture replay-meaningful
  tool calls per turn so a future repeat can replay against
  fresh data. Bounded by `MAX_THREAD_TOOL_CALL_TURNS`.
- `DiffBubble.tsx` (ship 4, renamed from `IncrementalBubble`)
  renders the no-movement / changed-since variants with a "↑
  turn N" scroll chip pointing back at the original answer.

**Trade explicitly accepted.** The diff path bypasses the
constitution gate. Justified: the input to delta-narration is a
typed `Delta` object grounded in real primitive output, no
fabrication surface. If a future ship adds a new failure class
for delta narration (e.g. interpretation error in the prose
layer), the gate surface needs to extend to
`dont_repeat_yourself`. Documented in ship 4's risks.

**Future ships expected to contribute.**

- Ship 5b: warehouse primitives gain `diff_spec()` declarations
  the same as the live primitives. Diff walker + narration call
  unchanged.
- Future ship: a soft TTL on the `IncrementalNoChange` bubble in
  UI (greys out after 60s of live-window slide) once dogfood
  confirms it's worth the complexity.
- Future ship: significance threshold per field-class (e.g.
  volume +0.05% is structurally a change but noise to a human).
  Today the threshold is the cross-check tolerance directly
  (10% for SOL volumes, exact for counts); tune from dogfood
  feedback before plumbing as config.

---

## How this doc grows

Every ship retro should include a "switches affected" line so
this doc stays in sync with the code. When a ship contributes:

1. Add a new bullet to the relevant switch's implementation map.
2. Reference the ship retro section in `00-build-order.md` so the
   git story stays linkable.
3. If the contribution is to the ship-5b-stub `ground_truth_match`,
   replace the "current status: stub" note with the real
   implementation map.
4. If the contribution introduces a genuinely new behavior class
   that none of the existing switches cover, add a new switch
   above. New switches require: (a) panel UI update, (b) tooltip
   text, (c) `AgentSwitches` field, (d) implementation map entry
   here.
5. If a contribution **retires** a switch (e.g. ship 5a retired
   `text_match`), keep the section in this doc with a brief
   "retired in ship N" header explaining why. Don't delete; the
   retirement story is part of the architecture history and
   future readers benefit from seeing what was tried and what
   replaced it.

The panel should stay at or below ~6 toggles for visitor
legibility. If a future contribution doesn't fit any existing
switch, evaluate whether the switch system itself needs
restructuring before adding a new toggle. New toggles should be
the last option; folding under existing switches is the first.

## Ship 5a meta: regex retirement

Ship 5a retired regex-on-prose from the load-bearing path. Before
ship 5a, the gate ran ~600 lines of regex machinery
(`policy_crosscheck::extract_from_text`,
`extract_from_claim`, `cross_check`, `prepare_text`,
`is_hedged_at`, `parse_match_value`, `multiplier_factor`,
`NUMBER_RE`) trying to interpret what numbers the model meant
in free-form prose: classify their unit, detect hedging, apply
multipliers, compare via tolerance. Every ship since 2.5 patched
a new failure mode in this layer (address-digit collisions in
2.6.1, byte-slice panics on multibyte chars in 4 dogfood,
paraphrase false-retracts that three other gate legs approved).

The architectural fix (locked in 5a): the model writes typed
citations (`${ref:N}` placeholders pointing at structured
`provenance` entries with `Number{metric, value}`); the gate
verifies citations structurally against the binding store
(typed lookup, no prose interpretation). Regex's only remaining
job is finding `${ref:N}` substrings  ASCII grammar we
control, not natural language we have to parse.

The principle, locked: **regex is fine when the input has a
known grammar; regex is wrong when the input is open-ended
natural language.** Citations are grammar (we control them).
Prose is open-ended (model controls it). So regex stays where
it parses tokens, leaves where it interpreted intent.

The trade explicitly accepted: bare audit-class numbers in
prose (e.g. "moved 50,000 SOL" without a chip) are caught by
the constitution LLM judge under `stay_in_role` reading meaning,
not by a deterministic detector. The user-facing disclaimer
footer carries the residual risk. Self-review pass +
orchestrator pattern (which would let the model re-read its own
output before committing) deferred to a future ship.
