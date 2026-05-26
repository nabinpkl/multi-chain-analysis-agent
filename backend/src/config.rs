use std::env;
use std::time::Duration;

#[derive(Clone, Debug)]
pub struct Config {
    pub port: u16,
    /// Internal HTTP listener port. Carries `/turn/*` and
    /// `/primitive/*` routes. NOT exposed to the host and must not be
    /// put behind any externally-facing reverse proxy or ingress; only
    /// reachable from sibling containers on the docker compose
    /// network. The agent-service container is the sole intended caller.
    pub internal_port: u16,
    pub cors_origin: String,
    pub clickhouse_url: String,
    pub clickhouse_db: String,
    pub clickhouse_user: String,
    pub clickhouse_password: String,
    pub solana_rpc_url: String,
    /// Minimum interval between calls on the ingester rate-limit lane
    /// (`getBlock`, `getSlot`). Sized to match Solana mainnet's slot
    /// production cadence so block ingestion stays responsive.
    pub rpc_ingester_min_interval: Duration,
    /// Minimum interval between calls on the primitive rate-limit lane
    /// (`getAccountInfo` from `/primitive/get_token_info`). Independent
    /// of the ingester lane so heavy agent traffic does not stall
    /// block ingestion. Defaults to a slower cadence than the ingester.
    pub rpc_primitive_min_interval: Duration,
    /// TTL for cached `token_metadata` rows, in slots. A read whose
    /// `fetched_at_slot` is more than this many slots behind the chain
    /// tip is treated as stale and re-fetched from RPC. Default 9000
    /// (~1 hour at Solana mainnet's 400 ms slot time). Becomes dead
    /// code once issue #48 (CDC instruction decoding) lands and the
    /// cache is kept fresh by ingest-time writes.
    pub metadata_cache_ttl_slots: u64,
    /// Comma-separated list of Host-header values the MCP route at
    /// `/mcp` will accept. Backstops the underlying network boundary
    /// against DNS-rebind attacks if this surface ever moves to a
    /// browser-reachable listener. Default covers loopback callers
    /// plus the docker compose service name `api` that the
    /// agent-service container uses internally. Allowlist entries
    /// without a port match any port; entries with a port (e.g.
    /// `example.com:8080`) match only that port.
    pub mcp_allowed_hosts: Vec<String>,
    pub kafka_brokers: String,
    pub kafka_topic_raw_edges: String,
    pub kafka_group_ch_sink: String,
    pub kafka_group_graph: String,
    pub kafka_auto_offset_reset: String,
    pub ch_sink_batch_size: usize,
    pub ch_sink_flush: Duration,
}

impl Config {
    pub fn from_env() -> Self {
        Self {
            port: env::var("PORT").ok().and_then(|p| p.parse().ok()).unwrap_or(8002),
            internal_port: env::var("INTERNAL_PORT")
                .ok()
                .and_then(|p| p.parse().ok())
                .unwrap_or(8004),
            cors_origin: env::var("CORS_ORIGIN").unwrap_or_else(|_| "*".into()),
            clickhouse_url: env::var("CLICKHOUSE_URL")
                .unwrap_or_else(|_| "http://localhost:8123".into()),
            clickhouse_db: env::var("CLICKHOUSE_DB").unwrap_or_else(|_| "multichain".into()),
            clickhouse_user: env::var("CLICKHOUSE_USER").unwrap_or_else(|_| "default".into()),
            clickhouse_password: env::var("CLICKHOUSE_PASSWORD").unwrap_or_default(),
            solana_rpc_url: env::var("SOLANA_RPC_URL").unwrap_or_default(),
            rpc_ingester_min_interval: Duration::from_millis(
                env::var("RPC_INGESTER_MIN_INTERVAL_MS")
                    .ok()
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(1000),
            ),
            rpc_primitive_min_interval: Duration::from_millis(
                env::var("RPC_PRIMITIVE_MIN_INTERVAL_MS")
                    .ok()
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(2000),
            ),
            metadata_cache_ttl_slots: env::var("METADATA_CACHE_TTL_SLOTS")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(9000),
            mcp_allowed_hosts: env::var("MCP_ALLOWED_HOSTS")
                .ok()
                .map(|s| {
                    s.split(',')
                        .map(|h| h.trim().to_string())
                        .filter(|h| !h.is_empty())
                        .collect()
                })
                .unwrap_or_else(|| {
                    vec![
                        "localhost".into(),
                        "127.0.0.1".into(),
                        "::1".into(),
                        "api".into(),
                    ]
                }),
            kafka_brokers: env::var("KAFKA_BROKERS")
                .unwrap_or_else(|_| "redpanda:9092".into()),
            kafka_topic_raw_edges: env::var("KAFKA_TOPIC_RAW_EDGES")
                .unwrap_or_else(|_| "solana.raw-edges".into()),
            kafka_group_ch_sink: env::var("KAFKA_GROUP_CH_SINK")
                .unwrap_or_else(|_| "ch-sink".into()),
            kafka_group_graph: env::var("KAFKA_GROUP_GRAPH")
                .unwrap_or_else(|_| "graph-engine".into()),
            kafka_auto_offset_reset: env::var("KAFKA_AUTO_OFFSET_RESET")
                .unwrap_or_else(|_| "latest".into()),
            ch_sink_batch_size: env::var("CH_SINK_BATCH_SIZE")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(1000),
            ch_sink_flush: Duration::from_secs(
                env::var("CH_SINK_FLUSH_SECS")
                    .ok()
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(5),
            ),
        }
    }
}
