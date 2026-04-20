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
    pub ingest_batch_size: usize,
    pub ingest_flush: Duration,
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
            ingest_batch_size: env::var("INGEST_BATCH_SIZE")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(10_000),
            ingest_flush: Duration::from_secs(
                env::var("INGEST_FLUSH_SECS")
                    .ok()
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(5),
            ),
        }
    }
}
