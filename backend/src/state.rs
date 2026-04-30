use std::sync::Arc;

use clickhouse::Client;
use parking_lot::RwLock;
use tokio::sync::{broadcast, watch};

use std::collections::HashMap;

use crate::agent::{
    AgentClient, AgentThread, BudgetGate, Ledger, OutputPolicy, PrimitiveRegistry, StubRegistry,
};
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

        // Stub registry first; policy + budget register their stubs
        // into it during construction. Pre-register per-primitive
        // and thread-state stubs so diagnostics lists them even
        // before anyone hits them.
        let agent_stubs = StubRegistry::new();
        let agent_policy = Arc::new(OutputPolicy::new(agent_stubs.clone()));
        let agent_budget = Arc::new(BudgetGate::new(agent_stubs.clone()));
        crate::agent::register_primitive_stubs(&agent_stubs);
        crate::agent::register_thread_stubs(&agent_stubs);
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
        };
        (state, analytics_senders)
    }
}
