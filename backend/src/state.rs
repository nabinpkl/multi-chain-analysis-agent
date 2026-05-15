use std::sync::Arc;
use std::sync::atomic::AtomicUsize;

use clickhouse::Client;
use dashmap::DashMap;
use parking_lot::RwLock;
use tokio::sync::{broadcast, mpsc, watch};

use crate::analytics::{AnalyticsChannels, AnalyticsSnapshot};
use crate::config::Config;
use crate::graph::GraphState;
use crate::graph::delta::GraphDelta;
use crate::graph::window::NUM_WINDOWS;
use crate::rpc::RpcClient;
use crate::snapshot::SnapshotCache;
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

/// Data-plane application state. Phase C dropped every agent-side
/// field (loop, ledger, registry, policy, budget, stubs, threads,
/// claims/bindings/switches/tool_calls per-session buffers,
/// debug_public). The Python agent service on `:8003` owns the agent
/// plane end-to-end; the only surface left here is what the data plane
/// needs to serve graph + primitive routes.
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
    /// Per-turn `WindowSnapshot` lease cache. Python opens a snapshot
    /// via `POST /turn/begin`, passes the returned `snapshot_id` on
    /// every primitive call this turn so reads are consistent across
    /// primitives, then releases via `POST /turn/end`. GC sweep drops
    /// anything older than 5 min.
    pub snapshot_cache: SnapshotCache,
    /// Solana RPC client. Shared between the ingester (which calls
    /// `getBlock` / `getSlot`) and primitive handlers (the
    /// `get_token_info` lazy fetcher calls `getAccountInfo`). The
    /// client carries two independent rate-limit lanes so the two
    /// consumers do not starve each other; see `rpc::client::RpcClient`.
    ///
    /// `None` when `SOLANA_RPC_URL` is unset (test/agent-only mode);
    /// any primitive that needs RPC returns 503 in that mode.
    pub rpc: Option<Arc<RpcClient>>,
    /// TTL for cached `token_metadata` rows, in slots. Sourced from
    /// `Config::metadata_cache_ttl_slots`. Carried on `AppState` so
    /// the `get_token_info` handler can pass it to the metadata
    /// fetcher's `CacheCtx` without re-reading env at each request.
    pub metadata_cache_ttl_slots: u64,
    /// Host-header allowlist enforced by the rmcp transport at
    /// `/mcp`. Sourced from `Config::mcp_allowed_hosts`. Carried
    /// here so `api::internal_router` builds the MCP service from
    /// state without re-reading env, matching how every other knob
    /// flows through `Config -> AppState -> route handlers`.
    pub mcp_allowed_hosts: Vec<String>,
    /// Per-snapshot claim channels used by the harness-engineering
    /// codex path. `turn_begin` creates an unbounded mpsc pair and
    /// stashes the sender here under the new snapshot_id; the MCP
    /// `emit_claims` tool looks up the sender by snapshot_id and
    /// pushes each parsed claim onto it; the Python loop driver
    /// drains via the SSE drain route at
    /// `GET /turn/{snapshot_id}/claims`; `turn_end` removes the
    /// entry, dropping the sender so any outstanding receiver sees
    /// end-of-stream.
    ///
    /// Unbounded because each turn emits at most a handful of chips
    /// (the agent's prompt budget caps it well below 100 in
    /// practice) and bounding would require backpressure semantics
    /// that complicate the in-MCP `#[tool]` body for no meaningful
    /// memory pressure relief.
    pub claim_channels: Arc<DashMap<String, mpsc::UnboundedSender<serde_json::Value>>>,
    /// Per-snapshot claim receivers parked here at `turn_begin` and
    /// taken by the SSE drain route on first subscribe. Wrapped in
    /// `Mutex<Option<...>>` so the drain route's `take()` enforces
    /// single-consumer at runtime: a second drain attempt on the
    /// same snapshot returns 409 instead of silently splitting the
    /// stream. `turn_end` removes whatever's left (whether taken
    /// or not), so a turn that never opens a drain stream still
    /// cleans up.
    pub claim_receivers: Arc<
        DashMap<String, parking_lot::Mutex<Option<mpsc::UnboundedReceiver<serde_json::Value>>>>,
    >,
    /// Per-snapshot tool-call budget counters. Incremented atomically
    /// on every budgeted MCP read tool dispatch (wallet_profile,
    /// community_summary, get_token_info); when the count reaches
    /// `turn_tool_call_budget`, the next dispatch short-circuits and
    /// returns a `no_more_lookups_this_turn` tool result instead of
    /// executing the primitive. The Python agent service has a
    /// symmetric in-process counter on the pydantic-ai runtime; this
    /// map is what the codex runtime relies on (codex speaks MCP
    /// directly to this server, so the per-turn cap has to live
    /// server-side). `turn_begin` inserts a fresh counter; `turn_end`
    /// removes it. The 5-minute snapshot GC sweep reaps orphans so a
    /// client that begins a turn but never calls `turn_end` doesn't
    /// leak entries. See `agent_service/policy/resource_bounds.py`
    /// for the symmetric Python side.
    pub tool_call_budgets: Arc<DashMap<String, AtomicUsize>>,
    /// Per-turn cap value (default 8, override via
    /// `AGENT_TURN_TOOL_CALL_BUDGET`). Read once at startup and
    /// stored here so every MCP dispatch reads a single source of
    /// truth without re-parsing env. Must match the Python side's
    /// `TURN_TOOL_CALL_BUDGET` constant or the two runtimes drift.
    pub turn_tool_call_budget: usize,
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

        // Build the RPC client once. The ingester and any primitive
        // that needs RPC clone the same `Arc<RpcClient>` from
        // `state.rpc`. The client carries two independent rate-limit
        // lanes (ingester + primitive) so heavy agent traffic does not
        // stall block ingestion.
        let rpc = if config.solana_rpc_url.is_empty() {
            None
        } else {
            Some(Arc::new(RpcClient::new(
                config.solana_rpc_url.clone(),
                config.rpc_ingester_min_interval,
                config.rpc_primitive_min_interval,
            )))
        };

        let state = Self {
            clickhouse,
            store: ch_store,
            tip: TipTracker::default(),
            deltas: WindowChannels::new(),
            analytics,
            graph: Arc::new(RwLock::new(GraphState::default())),
            snapshot_cache: SnapshotCache::new(),
            rpc,
            metadata_cache_ttl_slots: config.metadata_cache_ttl_slots,
            mcp_allowed_hosts: config.mcp_allowed_hosts.clone(),
            claim_channels: Arc::new(DashMap::new()),
            claim_receivers: Arc::new(DashMap::new()),
            tool_call_budgets: Arc::new(DashMap::new()),
            turn_tool_call_budget: std::env::var("AGENT_TURN_TOOL_CALL_BUDGET")
                .ok()
                .and_then(|s| s.parse::<usize>().ok())
                .unwrap_or(8),
        };
        (state, analytics_senders)
    }
}
