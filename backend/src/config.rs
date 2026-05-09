use std::env;
use std::time::Duration;

#[derive(Clone, Debug)]
pub struct Config {
    pub port: u16,
    pub cors_origin: String,
    pub clickhouse_url: String,
    pub clickhouse_db: String,
    pub clickhouse_user: String,
    pub clickhouse_password: String,
    pub solana_rpc_url: String,
    pub rpc_min_interval: Duration,
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
            cors_origin: env::var("CORS_ORIGIN").unwrap_or_else(|_| "*".into()),
            clickhouse_url: env::var("CLICKHOUSE_URL")
                .unwrap_or_else(|_| "http://localhost:8123".into()),
            clickhouse_db: env::var("CLICKHOUSE_DB").unwrap_or_else(|_| "multichain".into()),
            clickhouse_user: env::var("CLICKHOUSE_USER").unwrap_or_else(|_| "default".into()),
            clickhouse_password: env::var("CLICKHOUSE_PASSWORD").unwrap_or_default(),
            solana_rpc_url: env::var("SOLANA_RPC_URL").unwrap_or_default(),
            rpc_min_interval: Duration::from_millis(
                env::var("RPC_MIN_INTERVAL_MS")
                    .ok()
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(2000),
            ),
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
