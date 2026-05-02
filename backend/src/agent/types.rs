//! Wire types shared between agent backend and frontend. All exported via
//! ts-rs to `frontend/src/lib/generated/` so the boundary is type-safe.
//!
//! These are the locked-in wire shapes per the ship-1 plan. Future
//! ships consume them as additions only, never modifications.

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use ts_rs::TS;

// ============================================================================
// Agent request / context
// ============================================================================

/// User question + structured ground-truth context the frontend builds
/// from its own DOM/state. Per D-6 (overview), the context block is the
/// strongest disambiguation signal.
///
/// `thread_id` is None on the first send of a fresh conversation; the
/// backend mints one and returns it. Subsequent follow-up sends echo
/// the prior thread_id so the backend can append to the existing
/// in-memory thread (per ship 1.5). Refreshing the page or clicking
/// "new" clears the frontend's stored thread_id; the orphaned backend
/// thread is named by the `thread.in_memory_only` stub.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct AgentRequest {
    pub user_question: String,
    pub context: ViewContext,
    #[serde(default)]
    pub thread_id: Option<String>,
    /// Ship 3.5 ablation switches. Defaults reproduce the
    /// production preset (everything except the ground-truth
    /// stub). Frontend's "show builder view" lets visitors flip
    /// individual switches and observe the agent's behavior
    /// change.
    ///
    /// SECURITY: switches are reachable from any client. Project
    /// is a builder portfolio, not a product; we explicitly do
    /// not hide internals. If this code ever serves real
    /// end-user traffic, lock the switch surface server-side.
    #[serde(default)]
    pub switches: AgentSwitches,
    /// When true, backend emits `SseFrame::GatePath` frames so
    /// the frontend's builder view can render the executed path
    /// through the gate. Trace is always built and ledgered
    /// regardless; the toggle is wire-only.
    #[serde(default)]
    pub show_trace: bool,
}

/// Ship 3.5 ablation switches. Each field is a behavior contract;
/// when true, the agent has that behavior. Multiple ships may
/// contribute code that realizes a single switch (the switch is
/// the API surface, not the implementation). See
/// `docs/architecture/switches.md` for the implementation map.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct AgentSwitches {
    /// Identity, scope, conduct rules. With this off, the model
    /// is whatever the underlying LLM is. Realized today by the
    /// constitution leg + prompt v2 identity section + retry
    /// feedback loop.
    pub stay_in_role: bool,
    /// Numbers and entities in claims must come from real tool
    /// output. With this off, the model can invent values that
    /// no tool returned. Realized today by the binding leg.
    pub dont_fabricate: bool,
    /// Three sub-modes of consistency check across the chain:
    /// claim → prose → database. Sub-modes mix freely.
    pub cross_check: CrossCheckSwitches,
    /// Ship 4: agent recognizes repeat questions in the same
    /// thread, re-fetches the prior turn's primitives (live data
    /// may have moved), deterministically diffs against the
    /// prior outputs, and surfaces only what changed. With this
    /// off, the agent re-states everything from scratch every
    /// time. Realized today by `repeat_detector.rs` (small LLM
    /// pre-loop gate) + `diff.rs` (deterministic field walker
    /// reusing policy_crosscheck tolerance) + per-primitive
    /// `diff_spec()` declarations.
    ///
    /// Renamed from `incremental_answers` in a small post-ship-4
    /// readability pass: parallels `dont_fabricate` (negative-
    /// space behavior contract) and a viewer flipping it can
    /// predict the agent will repeat itself when it's off.
    #[serde(default = "default_true", alias = "incremental_answers")]
    pub dont_repeat_yourself: bool,
}

fn default_true() -> bool {
    true
}

/// `cross_check` sub-modes. Two independent toggles after ship 5a
/// retired `text_match` (regex on prose, brittle on paraphrase /
/// unicode). The remaining sub-modes are advisory in 5a's strict
/// merge (the load-bearing factuality check moved to structural
/// chip verification under `dont_fabricate`); `paraphrase_aware_match`
/// surfaces coherence issues, `ground_truth_match` is the ship 5b
/// stub for warehouse re-query.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct CrossCheckSwitches {
    /// LLM-driven coherence check: does the model's prose use its
    /// cited chip values consistently? Recall-based, paraphrase-
    /// robust. Reframed in ship 5a from "verifies factuality" to
    /// "verifies coherent prose around citations" once the
    /// structural chip-value gate took over the factuality role.
    /// Advisory: surfaces in path trace + breakdown, does not
    /// drive wire verdict on its own.
    pub paraphrase_aware_match: bool,
    /// Re-query the source-of-truth database, verify prose
    /// against actual data. NOT recall-based. Stub in ship 3.5;
    /// the real implementation lands in ship 5b with warehouse
    /// primitives. Today, flipping this on yields a path step
    /// with `NotApplicable { detail: "not implemented yet
    /// (lands in ship 5)" }`. The toggle exists so the panel
    /// shape is stable across the ship 5a → ship 5b transition.
    pub ground_truth_match: bool,
}

impl Default for AgentSwitches {
    fn default() -> Self {
        Self {
            stay_in_role: true,
            dont_fabricate: true,
            cross_check: CrossCheckSwitches::default(),
            // Ship 4 (originally `incremental_answers`, renamed to
            // `dont_repeat_yourself` post-ship for readability):
            // defaults true so the production preset includes the
            // don't-repeat behavior. Ship 4's preset list shifted
            // to make this the production default.
            dont_repeat_yourself: true,
        }
    }
}

impl Default for CrossCheckSwitches {
    fn default() -> Self {
        Self {
            paraphrase_aware_match: true,
            // Default OFF until ship 5b wires the warehouse
            // re-query path. Toggle is exposed so visitors can
            // see "this is where the project is going" without
            // a panel redesign when ship 5b lands.
            ground_truth_match: false,
        }
    }
}

/// One step in the gate's execution path. Built by `PathBuilder`
/// in `policy.rs` as each switch's leg runs (or skips). The
/// frontend's builder view renders these as a vertical timeline
/// inside `GatePathTimeline.tsx`.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct PathStep {
    /// Dotted stage id. Examples:
    /// - `"claim.stay_in_role"`
    /// - `"narrative.dont_fabricate"`
    /// - `"narrative.cross_check.text_match"`
    /// - `"narrative.cross_check.paraphrase_aware_match"`
    /// - `"narrative.cross_check.ground_truth_match"`
    pub stage: String,
    pub state: PathState,
    /// Wallclock microseconds the step took. For visual ordering
    /// only; determinism not promised across runs.
    pub elapsed_us: u32,
    /// Single-line human-readable note about what was checked
    /// and what the verdict was.
    pub note: String,
}

/// Wire mirror of `agent::policy::SubVerdict`. Mirrored here so
/// the wire / frontend types live together; `SubVerdict` stays
/// the working type inside `policy.rs`. `From<SubVerdict>`
/// lives in policy.rs.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[serde(tag = "state", rename_all = "snake_case")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum PathState {
    Approved,
    Retracted { reason: String },
    NotApplicable { detail: String },
}

/// Full path of a single channel's gate run. Emitted as
/// `SseFrame::GatePath` when `AgentRequest.show_trace=true`.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct GatePath {
    pub channel: String,
    pub switches: AgentSwitches,
    pub steps: Vec<PathStep>,
    pub final_verdict: PolicyVerdict,
}

#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct ViewContext {
    pub live_window_secs: u32,
    pub focus: Option<EntityRef>,
    pub selection: Vec<EntityRef>,
}

#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[serde(tag = "kind", content = "id", rename_all = "kebab-case")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum EntityRef {
    Wallet(String),
    Edge(String),
    Community(u32),
}

/// `session_id` is per-turn (drives the SSE GET, ledger row group).
/// `thread_id` is the persistent conversation handle the frontend
/// stores and echoes back on follow-up sends.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct AgentSessionStarted {
    pub session_id: String,
    pub thread_id: String,
    /// 0 on the first turn, increments on follow-ups. Frontend can
    /// surface this as "turn N" if useful.
    pub turn: u32,
}

/// Final SSE event for a session. `elapsed_ms` is u32 (caps at ~50d,
/// plenty) to dodge the bigint serialization headache.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct AgentDone {
    pub session_id: String,
    pub elapsed_ms: u32,
}

// ============================================================================
// Claim wire format (locked, per ship-1 plan)
// ============================================================================

/// Streamed analytical statement. The body uses `${ref:N}` placeholders
/// the frontend replaces with interactive chips at render time.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct Claim {
    /// ULID, sortable by emission order.
    pub id: String,
    pub session_id: String,
    pub kind: ClaimKind,
    /// 1-line plaintext (already escaped).
    pub headline: String,
    /// Structured paragraph; `${ref:N}` placeholders -> provenance chips.
    pub body_markdown: String,
    pub provenance: Vec<ProvenanceRef>,
    pub support_numbers: Vec<NumberRef>,
    /// None in v0; ship 5 populates for warehouse-derived historical.
    pub subgraph_slice: Option<SubgraphSlice>,
    /// Approved in v0 (stub policy). Ship 2 produces Retracted.
    pub policy_verdict: PolicyVerdict,
    /// Stubs that touched this claim's emission. Visible in the UI as
    /// "via stubs: ..." so historical claims keep their provenance even
    /// after stubs are removed.
    pub stubs_active: Vec<StubMarker>,
    /// Wallclock ms since session started. u32 to dodge bigint.
    pub emitted_at_ms: u32,
}

/// Closed enum: the renderer dispatches to per-kind cards. New variants
/// require a deliberate change. v0 only emits `Profile`; the rest exist
/// so ships 3/5/7 fill them without later refactor.
#[derive(Serialize, Deserialize, JsonSchema, TS, Debug, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum ClaimKind {
    Profile,
    Pattern,
    Comparison,
    Summary,
    Pulse,
}

/// Tagged reference back to a graph entity. The frontend's render-surface
/// derivation picks live highlight, modal, or inline chip based on the
/// ref shape (see plan: "Frontend render-surface derivation rule").
#[derive(Serialize, Deserialize, JsonSchema, TS, Debug, Clone)]
#[serde(tag = "kind", rename_all = "kebab-case")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum ProvenanceRef {
    /// `idx` is None when the wallet is not in the current live window
    /// (route to subgraph modal instead of live-graph chip).
    Wallet { addr: String, idx: Option<u32> },
    /// Stable id format: `"<edge_idx>:<gen>"`.
    Edge { id: String, src: u32, dst: u32 },
    Community { id: u32 },
    /// Populated by ship-5 warehouse primitives.
    TimeRange { from_s: u32, to_s: u32 },
    /// Aggregate metric reference. `support` lists EdgeIds backing the
    /// number so the user can drill in.
    Number {
        metric: String,
        value: f64,
        support: Vec<String>,
    },
}

#[derive(Serialize, Deserialize, JsonSchema, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct NumberRef {
    pub metric: String,
    pub value: f64,
}

/// Self-contained subgraph rendered on its own canvas in a modal.
/// Used by ship-5 for historical results that don't share layout
/// state with the live graph.
#[derive(Serialize, Deserialize, JsonSchema, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct SubgraphSlice {
    pub nodes: Vec<NodeSummary>,
    pub edges: Vec<EdgeSummary>,
    pub time_range: Option<TimeRangeWire>,
}

#[derive(Serialize, Deserialize, JsonSchema, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct NodeSummary {
    pub addr: String,
    pub role: Option<String>,
}

#[derive(Serialize, Deserialize, JsonSchema, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct EdgeSummary {
    pub src: String,
    pub dst: String,
    pub volume: f64,
}

#[derive(Serialize, Deserialize, JsonSchema, TS, Debug, Clone, Copy)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct TimeRangeWire {
    pub from_s: u32,
    pub to_s: u32,
}

/// Output-policy verdict (phase 03 layer 3). v0 is always Approved
/// because the policy gate is stubbed; ship 2 starts producing
/// `Retracted`.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[serde(tag = "verdict", rename_all = "kebab-case")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum PolicyVerdict {
    Approved,
    Retracted { reason: String },
}

// ============================================================================
// Stub visibility (ship-1 first-class foundation)
// ============================================================================

/// Per-claim badge: which stubs short-circuited during this claim's
/// emission. Persists into the claim history so stub provenance is
/// not lost when the global registry is later cleared.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct StubMarker {
    pub name: String,
    pub reason: String,
    pub promoted_in_ship: u8,
}

// ============================================================================
// Temporal + cost-class taxonomy
// ============================================================================

/// Mandatory temporal frame for primitives that have one (per D-6).
/// Externally tagged so `Live` serializes as the bare string `"live"`
/// (which is what models naturally produce for unit variants) and
/// `Range` as `{"range": {"from_s": ..., "to_s": ...}}`. The
/// internally-tagged form was tried first and the model misinterpreted
/// the schema, sending strings where objects were expected.
#[derive(Serialize, Deserialize, TS, JsonSchema, Debug, Clone)]
#[serde(rename_all = "kebab-case")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum TimeScope {
    /// Current rolling live window.
    Live,
    /// Absolute block-time range. Routes to warehouse path (ship-5).
    Range { from_s: u32, to_s: u32 },
}

/// Primitive data source family per D-5.
#[derive(Serialize, Deserialize, TS, Debug, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum DataSource {
    Live,
    Warehouse,
    External,
}

/// Cost-class tag on each primitive. Ship-4's budget gate reads this.
#[derive(Serialize, Deserialize, TS, Debug, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum CostClass {
    Cheap,
    Moderate,
    Expensive,
}

/// Wire-friendly mirror of `analytics::snapshot::NodeStats`. Defined
/// here (rather than ts-rs-deriving the analytics type) to keep the
/// agent's wire shapes self-contained.
///
/// Field names use the descriptive `*_volume_lamports` form (not the
/// internal `in_vol`/`out_vol` short keys) so that
/// `policy_crosscheck::classify_metric` resolves them to
/// `UnitClass::Sol` (substring match on "volume" + "lamport"). This
/// matters because `binding_store::build_binding` walks the JSON
/// output and stores each `Number` entry under whatever class
/// `classify_metric(field_name)` returns. With short keys, the
/// volumes landed in `UnitClass::Raw` and the structural value-compare
/// gate (`policy_structural::verify_chip_values`) silently skipped
/// them. Now they classify as Sol consistently end-to-end: primitive
/// JSON output, the model's `${ref:N}` Number provenance, and the
/// gate's lookup all use the same unit-class taxonomy.
#[derive(Serialize, Deserialize, JsonSchema, TS, Debug, Clone, Copy, Default)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct NodeStatsWire {
    pub degree: u32,
    pub total_volume_lamports: f64,
    pub in_volume_lamports: f64,
    pub out_volume_lamports: f64,
    pub bidir_volume_lamports: f64,
    pub sol_degree: u32,
    pub spl_degree: u32,
}

impl From<&crate::analytics::snapshot::NodeStats> for NodeStatsWire {
    fn from(s: &crate::analytics::snapshot::NodeStats) -> Self {
        Self {
            degree: s.degree,
            total_volume_lamports: s.volume,
            in_volume_lamports: s.in_vol,
            out_volume_lamports: s.out_vol,
            bidir_volume_lamports: s.bidir_vol,
            sol_degree: s.sol_degree,
            spl_degree: s.spl_degree,
        }
    }
}

// ============================================================================
// Ship 4: incremental answers (delta wire shape)
// ============================================================================
//
// On a repeat question, the agent re-fetches the prior turn's primitives,
// deterministically diffs the new outputs against the captured prior
// outputs, and surfaces ONLY what changed. The flat "I already said that"
// would be wrong on a live-data system; delta narration is the honest move.
//
// Determinism produces the typed `Delta`; the model narrates only the
// changed set. Same architectural pattern as the rest of this codebase:
// primitives produce data, narrative interprets.

/// One field's change between the prior turn's primitive output and the
/// freshly re-fetched output. Field paths are dotted (e.g. `"stats.volume"`,
/// `"top_counterparties"`); the diff walker emits one entry per changed
/// field-class match in the per-primitive `diff_spec`.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct FieldDelta {
    /// Dotted path within the primitive output (e.g.
    /// `"stats.in_volume_lamports"`, `"top_counterparties"`).
    pub field_path: String,
    /// Which primitive this field came from (e.g. `"wallet_profile"`).
    /// Surfaced so the builder-view chip can group deltas per
    /// primitive when a turn fired multiple.
    pub primitive: String,
    /// Typed change shape. `tag = "kind"` so the frontend can
    /// dispatch on a single discriminant.
    pub change: FieldChange,
}

/// Shape of a single field's change. Three variants matching the diff
/// walker's `FieldKind` strategies (Number, EntitySet, Count). The
/// `Ignore` strategy never produces a `FieldChange`.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[serde(tag = "kind", rename_all = "snake_case")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum FieldChange {
    /// Numeric field outside per-class tolerance. `pct` is the
    /// signed percent change (current - prior) / prior, or 0.0
    /// when prior is 0.
    NumberMoved {
        prior: f64,
        current: f64,
        pct: f64,
    },
    /// Entity-list field where membership changed. `added` and
    /// `removed` carry the keys (typically wallet addresses).
    SetChanged {
        added: Vec<String>,
        removed: Vec<String>,
    },
    /// Count-class field where any delta is meaningful.
    CountChanged {
        prior: f64,
        current: f64,
    },
}

/// Full diff result handed to the narrative-on-delta call (or used
/// to short-circuit when empty). `unchanged_field_count` is for the
/// builder-view chip "2 changed / 4 unchanged"; only the structurally
/// changed fields surface as `FieldDelta` entries.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct Delta {
    pub changed: Vec<FieldDelta>,
    pub unchanged_field_count: u32,
}

/// SSE payload emitted when the `dont_repeat_yourself` switch
/// fires (repeat detected) AND the deterministic diff produced no
/// structural changes. No LLM narrative call happens on this path;
/// the bubble exists so the user sees closure ("we covered this in
/// turn N, no movement since") rather than silence.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct NoMovement {
    pub prior_turn: u32,
    /// Primitive names re-fetched and confirmed unchanged. Used by
    /// the bubble for honesty ("re-checked wallet_profile,
    /// nothing moved").
    pub primitives_replayed: Vec<String>,
}

/// SSE payload emitted when the `dont_repeat_yourself` switch
/// fires AND the deterministic diff produced changes. Carries the
/// typed `Delta` + a small narrative call's prose describing only
/// what shifted since the prior turn. Both ship together so the
/// frontend can render either the prose or the structured chips.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct ChangedSince {
    pub prior_turn: u32,
    pub delta: Delta,
    pub prose: String,
}

// ============================================================================
// Ship 5a: narrative with citation provenance
// ============================================================================
//
// The narrative channel grew an in-band citation grammar in ship 5a. Today
// `text` may contain `${ref:N}` placeholder tokens; `provenance[N]` is the
// typed entry the chip resolves against. The frontend's narrative bubble
// renders chips at every placeholder, same way Claim profile cards already
// do. The deterministic gate validates that every `${ref:N}` resolves and
// every `ProvenanceRef::Number` traces back to the binding store.
//
// Provenance is assembled by the loop at narrative-emit time from the
// claims emitted this turn (concatenated provenance arrays). The model
// uses `${ref:N}` indices that count across the assembled array in
// emission order; prompt v4 documents the rule.

/// Narrative channel payload as of ship 5a. Replaces the prior
/// `SseFrame::Narrative { text: String }` shape so the frontend has
/// the typed ProvenanceRef array it needs to render `${ref:N}` chips.
/// AGENTS.md "no compat layers" applies; the old shape is gone.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct NarrativeWithRefs {
    /// Free-form narrative text. May contain inline `${ref:N}` tokens
    /// the renderer resolves against `provenance`.
    pub text: String,
    /// Typed citation array assembled by the loop from this turn's
    /// emitted Claims (concatenated provenance arrays in emission
    /// order). Index N in `${ref:N}` resolves against this vec.
    pub provenance: Vec<ProvenanceRef>,
}
