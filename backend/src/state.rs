use std::sync::Arc;

use clickhouse::Client;
use parking_lot::RwLock;
use tokio::sync::{broadcast, watch};

use std::collections::HashMap;

use crate::agent::{
    AgentClient, AgentThread, BudgetGate, Ledger, OutputPolicy, PrimitiveRegistry, StubRegistry,
};
use crate::agent::primitives::PrimitiveBindingStore;
use crate::agent::types::{AgentSwitches, Claim};
use crate::analytics::{AnalyticsChannels, AnalyticsSnapshot};
use crate::api::agent::AgentSessions;
use crate::config::Config;
use crate::graph::GraphState;
use crate::graph::delta::GraphDelta;
use crate::graph::window::NUM_WINDOWS;
use crate::store::EdgeStore;
use crate::store::clickhouse_store::ClickHouseEdgeStore;
use crate::tip::TipTracker;

/// Delta broadcast channel capacity per window.
const DELTA_BROADCAST_CAPACITY: usize = 4096;

/// Per-window broadcast senders. One channel per rolling window
/// (60s, 300s, 900s, 1800s, 3600s) so each subscriber sees only the
/// deltas relevant to its window.
#[derive(Clone)]
pub struct WindowChannels {
    pub txs: [broadcast::Sender<Arc<Vec<GraphDelta>>>; NUM_WINDOWS],
}

impl WindowChannels {
    pub fn new() -> Self {
        let txs = std::array::from_fn(|_| broadcast::channel(DELTA_BROADCAST_CAPACITY).0);
        Self { txs }
    }

    pub fn sender(&self, window_idx: usize) -> &broadcast::Sender<Arc<Vec<GraphDelta>>> {
        &self.txs[window_idx]
    }
}

impl Default for WindowChannels {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Clone)]
pub struct AppState {
    pub clickhouse: Client,
    pub store: Arc<dyn EdgeStore>,
    pub tip: TipTracker,
    /// Per-window delta broadcast. Subscribers bind to one window's
    /// channel based on the `?window=` query param.
    pub deltas: WindowChannels,
    /// Per-window analytics broadcast + latest snapshot watch. Read-side
    /// only; the corresponding `watch::Sender` array is owned by the
    /// analytics tasks (see `analytics::spawn_all`).
    pub analytics: AnalyticsChannels,
    /// In-memory graph engine: node interner + adjacency + Union-Find.
    pub graph: Arc<RwLock<GraphState>>,
    /// rig-backed LLM client. `None` when `AGENT_API_KEY` is unset;
    /// agent endpoints return 503 in that case so the rest of the
    /// server still boots and serves /graph/stream.
    pub agent_client: Option<AgentClient>,
    /// In-memory pending-session map for POST /agent/ask -> SSE
    /// /agent/stream/:id handoff.
    pub agent_sessions: AgentSessions,
    /// Action ledger writer (per phase 04). Sync writes in v0.
    pub agent_ledger: Ledger,
    /// Tool registry. Built once at startup; immutable thereafter.
    pub agent_registry: Arc<PrimitiveRegistry>,
    /// Output-policy gate (phase 03 layer 3). v0 stub: always Approved.
    pub agent_policy: Arc<OutputPolicy>,
    /// Cost-rate-limit gate (phase 05). v0 stub: always Ok.
    pub agent_budget: Arc<BudgetGate>,
    /// Stub registry surfaced via /agent/diagnostics + the UI banner.
    pub agent_stubs: Arc<StubRegistry>,
    /// Per-thread message history for follow-up continuity (ship 1.5).
    /// Backend-owned single source of truth; frontend echoes thread_id.
    /// In-memory; named by `thread.in_memory_only` stub.
    pub agent_threads: Arc<parking_lot::Mutex<HashMap<String, AgentThread>>>,
    /// Per-session buffer of approved Claims emitted this turn. Ship 2
    /// reads this in the loop driver after rig's prompt() returns so
    /// the Narrative gate sees what the agent already cited. Drained
    /// at end-of-turn so the map can't leak across turns. Keyed by
    /// session_id (per-turn handle, NOT thread_id, because each turn
    /// gets a fresh narrative gate scope).
    pub agent_claims_emitted: Arc<parking_lot::Mutex<HashMap<String, Vec<Claim>>>>,
    /// Ship 3 per-session primitive-binding buffer. Loop session-start
    /// loads `thread.bindings` here keyed by session_id; each
    /// primitive dispatch appends; emit_claim's `check_claim` and
    /// the loop's `check_narrative` both read from it; loop
    /// session-end writes back to `thread.bindings` and drains.
    /// Same per-turn lifecycle pattern as `agent_claims_emitted` so
    /// session_id is the only handle surfaces (claim emission, tool
    /// dispatch, gate) need.
    pub agent_bindings: Arc<parking_lot::Mutex<HashMap<String, PrimitiveBindingStore>>>,
    /// Ship 3.5 per-session ablation switches. Loop session-start
    /// pulls switches off the request and stores here keyed by
    /// session_id; emit_claim's `check_claim` and the loop's
    /// `check_narrative` both read from it; loop session-end
    /// drains. Mirrors the `agent_bindings` pattern so primitives
    /// only need session_id to look up gate config.
    pub agent_switches: Arc<parking_lot::Mutex<HashMap<String, AgentSwitches>>>,
    /// Ship 3.5 per-session `show_trace` flag. True when the
    /// request asked for the builder view; gates whether
    /// `SseFrame::GatePath` frames are pushed onto the SSE wire.
    /// Path is always built and ledgered regardless; this is
    /// wire-only.
    pub agent_show_trace: Arc<parking_lot::Mutex<HashMap<String, bool>>>,
    /// Ship 4 per-session tool-call recorder. The PrimitiveAdapter
    /// appends a `TurnToolCallRecord` here for every primitive
    /// dispatch whose `diff_spec()` is non-empty (i.e. primitives
    /// whose outputs are replay-meaningful; emit_claim is naturally
    /// excluded). At session end the loop drains this buffer into
    /// `AgentThread.tool_calls_per_turn[turn]` so a future repeat-
    /// of-this-turn can replay against fresh data. Same per-turn
    /// lifecycle pattern as `agent_bindings`.
    pub agent_tool_calls: Arc<
        parking_lot::Mutex<HashMap<String, Vec<crate::agent::TurnToolCallRecord>>>,
    >,
    /// Ship 2.6.1 dev-mode toggle. When true, SSE error / narrative-
    /// retracted frames carry diagnostic detail fields the frontend
    /// renders inline. When false (prod default), wire is fully
    /// scrubbed of internal terms (rig, OpenRouter, constitution
    /// reasons, etc.). Driven by `AGENT_DEBUG_PUBLIC` env. Solo-dev
    /// pattern: the UI is the only surface I check, so dev-mode
    /// surfaces rare events on the UI itself; prod ships sterile.
    pub agent_debug_public: bool,
}

impl AppState {
    /// Build the read-side state plus the per-window analytics
    /// `watch::Sender` array. The senders are consumed by
    /// `analytics::spawn_all` so each window-task owns its push side
    /// and `AppState` only carries the receiver side.
    pub fn new(
        config: &Config,
    ) -> (
        Self,
        [watch::Sender<Arc<AnalyticsSnapshot>>; NUM_WINDOWS],
    ) {
        let clickhouse = Client::default()
            .with_url(&config.clickhouse_url)
            .with_user(&config.clickhouse_user)
            .with_password(&config.clickhouse_password)
            .with_database(&config.clickhouse_db);

        let ch_store = Arc::new(ClickHouseEdgeStore::new(clickhouse.clone()));
        let (analytics, analytics_senders) = AnalyticsChannels::new();

        let agent_config = crate::agent::AgentConfig::from_env();
        let agent_client = crate::agent::build_client(&agent_config);

        // Stub registry first. Budget still registers its stub during
        // construction (ship 4 retires it). Output policy used to
        // register `policy.always_approve` here; ship 2 retired the
        // stub when the cheap-model gate became real, so policy no
        // longer touches the registry on construction.
        // Pre-register per-primitive, thread-state, and narrative
        // stubs so diagnostics lists them before anyone hits them.
        let agent_stubs = StubRegistry::new();
        // OutputPolicy accepts an Option<AgentClient>; when None the
        // gate auto-approves at the (unreachable) call site. Keeps
        // the AppState field non-Option so callers don't have to
        // re-check. Ship 2.5 dropped the StubRegistry parameter when
        // `narrative.no_numerical_crosscheck` retired.
        let agent_policy = Arc::new(OutputPolicy::new(agent_client.clone()));
        let agent_budget = Arc::new(BudgetGate::new(agent_stubs.clone()));
        crate::agent::register_primitive_stubs(&agent_stubs);
        crate::agent::register_thread_stubs(&agent_stubs);
        // `register_narrative_stubs` retired in ship 2.5: the cross-check
        // landed and there's no remaining narrative-gating stub to flag.
        let agent_registry = Arc::new(crate::agent::build_registry());
        let agent_ledger = Ledger::new(clickhouse.clone());

        let state = Self {
            clickhouse,
            store: ch_store,
            tip: TipTracker::default(),
            deltas: WindowChannels::new(),
            analytics,
            graph: Arc::new(RwLock::new(GraphState::default())),
            agent_client,
            agent_sessions: AgentSessions::new(),
            agent_ledger,
            agent_registry,
            agent_policy,
            agent_budget,
            agent_stubs,
            agent_threads: Arc::new(parking_lot::Mutex::new(HashMap::new())),
            agent_claims_emitted: Arc::new(parking_lot::Mutex::new(HashMap::new())),
            agent_bindings: Arc::new(parking_lot::Mutex::new(HashMap::new())),
            agent_switches: Arc::new(parking_lot::Mutex::new(HashMap::new())),
            agent_show_trace: Arc::new(parking_lot::Mutex::new(HashMap::new())),
            agent_tool_calls: Arc::new(parking_lot::Mutex::new(HashMap::new())),
            agent_debug_public: agent_config.debug_public,
        };
        (state, analytics_senders)
    }
}
