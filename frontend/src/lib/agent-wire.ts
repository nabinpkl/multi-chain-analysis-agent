/* eslint-disable */
/**
 * AUTO-GENERATED FILE -- DO NOT EDIT.
 *
 * Source of truth: agent-service/src/agent_service/wire/agent.py
 * Generator: frontend/scripts/build-agent-wire.mjs (json-schema-to-typescript)
 * Re-run via: `just regen-wire-types`
 *
 * Drift between this file and the pydantic source fails CI via
 * agent-service/tests/integration/test_codegen_drift.py.
 */

export type ElapsedMs = number;
export type SessionId = string;
export type Focus = (EntityRefWallet | EntityRefEdge | EntityRefCommunity) | null;
export type Id = string;
export type Kind = "wallet";
export type Id1 = string;
export type Kind1 = "edge";
export type Id2 = number;
export type Kind2 = "community";
export type LiveWindowSecs = number;
export type Selection = (EntityRefWallet | EntityRefEdge | EntityRefCommunity)[];
export type ShowTrace = boolean;
export type GroundTruthMatch = boolean;
export type ParaphraseAwareMatch = boolean;
export type DontFabricate = boolean;
export type DontRepeatYourself = boolean;
export type StayInRole = boolean;
export type ThreadId = string | null;
export type UserQuestion = string;
export type SessionId1 = string;
export type ThreadId1 = string;
export type Turn = number;
export type Change = FieldChangeNumberMoved | FieldChangeSetChanged | FieldChangeCountChanged;
export type Current = number;
export type Kind3 = "number_moved";
export type Pct = number;
export type Prior = number;
export type Added = string[];
export type Kind4 = "set_changed";
export type Removed = string[];
export type Current1 = number;
export type Kind5 = "count_changed";
export type Prior1 = number;
export type FieldPath = string;
export type Primitive = string;
export type Changed = FieldDelta[];
export type UnchangedFieldCount = number;
export type PriorTurn = number;
export type Prose = string;
/**
 * Structured paragraph; ${ref:N} placeholders -> provenance chips.
 */
export type BodyMarkdown = string;
/**
 * Wallclock ms since session started. u32 in Rust.
 */
export type EmittedAtMs = number;
/**
 * One-line plaintext headline.
 */
export type Headline = string;
/**
 * ULID, sortable by emission order.
 */
export type Id3 = string;
/**
 * Closed enum: the renderer dispatches to per-kind cards. New variants require a deliberate change. v0 only emits `Profile`; the rest exist so ships 3/5/7 fill them without later refactor.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "ClaimKind".
 */
export type ClaimKind = "profile" | "pattern" | "comparison" | "summary" | "pulse";
export type PolicyVerdict = PolicyVerdictApproved | PolicyVerdictRetracted;
export type Verdict = "approved";
export type Reason = string;
export type Verdict1 = "retracted";
/**
 * Tagged reference back to a graph entity. The frontend's render-surface derivation picks live highlight, modal, or inline chip based on the ref shape (see plan: "Frontend render-surface derivation rule").
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "ProvenanceRef".
 */
export type ProvenanceRef = ProvenanceRef11 | ProvenanceRef12 | ProvenanceRef13 | ProvenanceRef14 | ProvenanceRef15;
export type Addr = string;
export type Idx = number | null;
/**
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "Kind".
 */
export type Kind6 = "wallet";
export type Dst = number;
export type Id4 = string;
/**
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "Kind11".
 */
export type Kind11 = "edge";
export type Src = number;
export type Id5 = number;
/**
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "Kind12".
 */
export type Kind12 = "community";
export type FromS = number;
/**
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "Kind13".
 */
export type Kind13 = "time-range";
export type ToS = number;
/**
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "Kind14".
 */
export type Kind14 = "number";
export type Metric = string;
export type Support = string[];
export type Value = number;
export type Provenance = ProvenanceRef[];
export type SessionId2 = string;
export type Name = string;
export type PromotedInShip = number;
export type Reason1 = string;
export type StubsActive = StubMarker[];
export type Dst1 = string;
export type Src1 = string;
export type Volume = number;
export type Edges = EdgeSummary[];
export type Addr1 = string;
export type Role = string | null;
export type Nodes = NodeSummary[];
export type FromS1 = number;
export type ToS1 = number;
export type Metric1 = string;
export type Value1 = number;
export type SupportNumbers = NumberRef[];
export type Phrase = string;
export type UnitClass = string;
export type Value2 = number;
export type ClaimNumbers = LlmExtractedNumber[];
export type NarrativeNumbers = LlmExtractedNumber[];
export type Reason2 = string;
export type Verdict2 = "approve" | "retract" | "reject";
export type DebugMessage = string | null;
export type Message = string;
export type Channel = string;
export type FinalVerdict = PolicyVerdictApproved | PolicyVerdictRetracted;
export type ElapsedUs = number;
/**
 * Single-line note: what was checked, what verdict.
 */
export type Note = string;
export type Stage = string;
export type State = PathStateApproved | PathStateRetracted | PathStateNotApplicable;
export type State1 = "approved";
export type Reason3 = string;
export type State2 = "retracted";
export type Detail = string;
export type State3 = "not_applicable";
export type Steps = PathStep[];
export type DebugReason = string | null;
export type Reason4 = string;
export type Text = string;
export type Provenance1 = ProvenanceRef[];
export type Text1 = string;
export type PrimitivesReplayed = string[];
export type PriorTurn1 = number;
export type Detail1 = string;
export type Phase = string;

export interface AgentDone {
  elapsed_ms: ElapsedMs;
  session_id: SessionId;
}
/**
 * User question + ViewContext. `thread_id` is None on the first
 * send of a fresh conversation; the backend mints one and returns
 * it. Subsequent follow-ups echo the prior thread_id.
 *
 * `switches` defaults to the production preset (everything except
 * `ground_truth_match`). `show_trace` controls whether `GatePath`
 * frames are emitted; trace is always built and ledgered regardless.
 *
 * SECURITY note (mirrored from Rust): switches are reachable from
 * any client. Project is a builder portfolio, not a product; we
 * explicitly do not hide internals. If this code ever serves real
 * end-user traffic, lock the switch surface server-side.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "AgentRequest".
 */
export interface AgentRequest {
  context: ViewContext;
  show_trace?: ShowTrace;
  switches?: AgentSwitches;
  thread_id?: ThreadId;
  user_question: UserQuestion;
}
/**
 * Structured ground-truth context the frontend builds from its own
 * DOM/state. Per D-6 (overview), the context block is the strongest
 * disambiguation signal.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "ViewContext".
 */
export interface ViewContext {
  focus?: Focus;
  live_window_secs: LiveWindowSecs;
  selection?: Selection;
}
/**
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "EntityRefWallet".
 */
export interface EntityRefWallet {
  id: Id;
  kind?: Kind;
}
/**
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "EntityRefEdge".
 */
export interface EntityRefEdge {
  id: Id1;
  kind?: Kind1;
}
/**
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "EntityRefCommunity".
 */
export interface EntityRefCommunity {
  id: Id2;
  kind?: Kind2;
}
/**
 * Ship 3.5 ablation switches. Each field is a behavior contract;
 * when true, the agent has that behavior. Defaults reproduce the
 * production preset.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "AgentSwitches".
 */
export interface AgentSwitches {
  cross_check?: CrossCheckSwitches;
  dont_fabricate?: DontFabricate;
  dont_repeat_yourself?: DontRepeatYourself;
  stay_in_role?: StayInRole;
}
/**
 * `cross_check` sub-modes. Two independent toggles after ship 5a
 * retired `text_match`. Both advisory in the strict merge.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "CrossCheckSwitches".
 */
export interface CrossCheckSwitches {
  ground_truth_match?: GroundTruthMatch;
  paraphrase_aware_match?: ParaphraseAwareMatch;
}
/**
 * Returned synchronously from POST /agent/ask. `session_id` is
 * per-turn (drives the SSE GET, ledger row group). `thread_id`
 * is the persistent conversation handle. `turn` is 0 on the first
 * turn, increments on follow-ups.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "AgentSessionStarted".
 */
export interface AgentSessionStarted {
  session_id: SessionId1;
  thread_id: ThreadId1;
  turn?: Turn;
}
/**
 * Emitted when `dont_repeat_yourself` fires AND the diff is
 * non-empty. Carries both the typed `Delta` and the small
 * narrative call's prose. Frontend can render either.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "ChangedSince".
 */
export interface ChangedSince {
  delta: Delta;
  prior_turn: PriorTurn;
  prose: Prose;
}
/**
 * Full diff result. `unchanged_field_count` powers the builder-view
 * chip ('2 changed / 4 unchanged'); only structurally-changed fields
 * appear in `changed`.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "Delta".
 */
export interface Delta {
  changed: Changed;
  unchanged_field_count: UnchangedFieldCount;
}
/**
 * One field's change between prior turn's primitive output and the
 * freshly re-fetched output. `field_path` is dotted (e.g.
 * `stats.in_volume_lamports`). `primitive` is the producing primitive
 * name (e.g. `wallet_profile`).
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "FieldDelta".
 */
export interface FieldDelta {
  change: Change;
  field_path: FieldPath;
  primitive: Primitive;
}
/**
 * Numeric field outside per-class tolerance. `pct` is the signed
 * percent change; 0.0 when prior is 0.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "FieldChangeNumberMoved".
 */
export interface FieldChangeNumberMoved {
  current: Current;
  kind?: Kind3;
  pct: Pct;
  prior: Prior;
}
/**
 * Entity-list field where membership changed. `added`/`removed`
 * carry the keys (typically wallet addresses).
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "FieldChangeSetChanged".
 */
export interface FieldChangeSetChanged {
  added: Added;
  kind?: Kind4;
  removed: Removed;
}
/**
 * Count-class field where any delta is meaningful.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "FieldChangeCountChanged".
 */
export interface FieldChangeCountChanged {
  current: Current1;
  kind?: Kind5;
  prior: Prior1;
}
/**
 * Streamed analytical statement. The body uses `${ref:N}`
 * placeholders the frontend replaces with interactive chips at
 * render time. `provenance[N]` is the typed entry the chip
 * resolves against.
 *
 * Wire shape locked to match Rust `agent::types::Claim` byte-for-byte.
 * Drift surfaces as failed Phase I.5 SSE golden parse tests.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "Claim".
 */
export interface Claim {
  body_markdown: BodyMarkdown;
  emitted_at_ms: EmittedAtMs;
  headline: Headline;
  id: Id3;
  kind: ClaimKind;
  policy_verdict: PolicyVerdict;
  provenance: Provenance;
  session_id: SessionId2;
  stubs_active: StubsActive;
  subgraph_slice?: SubgraphSlice | null;
  support_numbers: SupportNumbers;
}
/**
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "PolicyVerdictApproved".
 */
export interface PolicyVerdictApproved {
  verdict?: Verdict;
}
/**
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "PolicyVerdictRetracted".
 */
export interface PolicyVerdictRetracted {
  reason: Reason;
  verdict?: Verdict1;
}
/**
 * `idx` is None when the wallet is not in the current live window (route to subgraph modal instead of live-graph chip).
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "ProvenanceRef11".
 */
export interface ProvenanceRef11 {
  addr: Addr;
  idx?: Idx;
  kind: Kind6;
}
/**
 * Stable id format: `"<edge_idx>:<gen>"`.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "ProvenanceRef12".
 */
export interface ProvenanceRef12 {
  dst: Dst;
  id: Id4;
  kind: Kind11;
  src: Src;
}
/**
 * Tagged reference back to a graph entity. The frontend's render-surface derivation picks live highlight, modal, or inline chip based on the ref shape (see plan: "Frontend render-surface derivation rule").
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "ProvenanceRef13".
 */
export interface ProvenanceRef13 {
  id: Id5;
  kind: Kind12;
}
/**
 * Populated by ship-5 warehouse primitives.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "ProvenanceRef14".
 */
export interface ProvenanceRef14 {
  from_s: FromS;
  kind: Kind13;
  to_s: ToS;
}
/**
 * Aggregate metric reference. `support` lists EdgeIds backing the number so the user can drill in.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "ProvenanceRef15".
 */
export interface ProvenanceRef15 {
  kind: Kind14;
  metric: Metric;
  support: Support;
  value: Value;
}
/**
 * Per-claim badge: which stubs short-circuited during this claim's
 * emission. Persists into history so stub provenance survives even
 * after the global registry is cleared.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "StubMarker".
 */
export interface StubMarker {
  name: Name;
  promoted_in_ship: PromotedInShip;
  reason: Reason1;
}
/**
 * Self-contained subgraph rendered on its own canvas in a modal. Used by ship-5 for historical results that don't share layout state with the live graph.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "SubgraphSlice".
 */
export interface SubgraphSlice {
  edges: Edges;
  nodes: Nodes;
  time_range?: TimeRangeWire | null;
}
/**
 * Subgraph edge row. `volume` in lamports.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "EdgeSummary".
 */
export interface EdgeSummary {
  dst: Dst1;
  src: Src1;
  volume: Volume;
}
/**
 * Subgraph node row. `role` is None when unclassified.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "NodeSummary".
 */
export interface NodeSummary {
  addr: Addr1;
  role?: Role;
}
/**
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "TimeRangeWire".
 */
export interface TimeRangeWire {
  from_s: FromS1;
  to_s: ToS1;
}
/**
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "NumberRef".
 */
export interface NumberRef {
  metric: Metric1;
  value: Value1;
}
/**
 * Constitution v3 response shape. `verdict` is one of three
 * strings the policy prompt prescribes; `reason` defaults to "" so
 * a malformed older-style response still parses cleanly. `extraction`
 * is None when the LLM omitted the sidecar entirely.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "ConstitutionVerdict".
 */
export interface ConstitutionVerdict {
  extraction?: LlmExtraction | null;
  reason?: Reason2;
  verdict: Verdict2;
}
/**
 * Constitution gate's structured sidecar. Numbers the LLM saw in
 * narrative + claim text, classified by unit. The structural
 * cross-check pairs these against the binding store.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "LlmExtraction".
 */
export interface LlmExtraction {
  claim_numbers?: ClaimNumbers;
  narrative_numbers?: NarrativeNumbers;
}
/**
 * LLM-side extracted number from constitution gate's `extraction`
 * JSON sidecar. `phrase` is debugging context only; surfaces in
 * dev-mode debug fields and is discarded during compare.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "LlmExtractedNumber".
 */
export interface LlmExtractedNumber {
  phrase?: Phrase;
  unit_class: UnitClass;
  value: Value2;
}
/**
 * Terminal turn-level error. The SSE handler renders this as an
 * `Error` event before the closing `Done`. `debug_message` only
 * populated when AGENT_DEBUG_PUBLIC=1.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "Error".
 */
export interface Error {
  debug_message?: DebugMessage;
  message: Message;
}
/**
 * Full path of one channel's gate run. Emitted as `GatePath` SSE
 * frame when `AgentRequest.show_trace=true`. Trace is always built
 * and ledgered; the frame is wire-only.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "GatePath".
 */
export interface GatePath {
  channel: Channel;
  final_verdict: FinalVerdict;
  steps: Steps;
  switches: AgentSwitches;
}
/**
 * One step in the gate's execution path. Stage is a dotted id
 * (e.g. `claim.stay_in_role`, `narrative.cross_check.paraphrase_aware_match`).
 * `elapsed_us` is wallclock microseconds; ordering only, determinism
 * not promised.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "PathStep".
 */
export interface PathStep {
  elapsed_us: ElapsedUs;
  note: Note;
  stage: Stage;
  state: State;
}
/**
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "PathStateApproved".
 */
export interface PathStateApproved {
  state?: State1;
}
/**
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "PathStateRetracted".
 */
export interface PathStateRetracted {
  reason: Reason3;
  state?: State2;
}
/**
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "PathStateNotApplicable".
 */
export interface PathStateNotApplicable {
  detail: Detail;
  state?: State3;
}
/**
 * Narrative the constitution gate retracted. Carries the original
 * text alongside a friendly user-facing `reason`. `debug_reason`
 * only populated when AGENT_DEBUG_PUBLIC=1 (dev-mode); absent in
 * prod so the wire stays sterile.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "NarrativeRetracted".
 */
export interface NarrativeRetracted {
  debug_reason?: DebugReason;
  reason: Reason4;
  text: Text;
}
/**
 * Approved free-form prose. May contain inline `${ref:N}` tokens
 * the renderer resolves against `provenance` (assembled by the loop
 * from this turn's emitted Claims, concatenated provenance arrays
 * in emission order).
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "NarrativeWithRefs".
 */
export interface NarrativeWithRefs {
  provenance: Provenance1;
  text: Text1;
}
/**
 * Emitted when `dont_repeat_yourself` fires AND the diff is empty.
 * No LLM narrative call happens on this path; the bubble exists so
 * the user sees closure ('we covered this in turn N, no movement
 * since').
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "NoMovement".
 */
export interface NoMovement {
  primitives_replayed: PrimitivesReplayed;
  prior_turn: PriorTurn1;
}
/**
 * Lightweight progress ping. Phase + detail are free-form strings
 * the frontend can render as a status line.
 *
 * This interface was referenced by `AgentWire`'s JSON-Schema
 * via the `definition` "Progress".
 */
export interface Progress {
  detail: Detail1;
  phase: Phase;
}
