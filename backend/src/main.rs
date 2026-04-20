use std::net::SocketAddr;

use tokio::sync::watch;
use tower_http::cors::CorsLayer;
use tower_http::trace::TraceLayer;
use tracing::{error, info, warn};

mod api;
mod config;
mod domain;
mod ingest;
mod rpc;
mod state;
mod store;
mod tip;

use config::Config;
use ingest::IngestConfig;
use rpc::RpcClient;
use state::AppState;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    init_tracing();

    let config = Config::from_env();
    info!(?config.clickhouse_url, ?config.clickhouse_db, "loaded config");

    let state = AppState::new(&config);

    store::schema::bootstrap(&state.clickhouse).await?;
    info!("clickhouse schema bootstrapped");

    let cors = if config.cors_origin == "*" {
        CorsLayer::permissive()
    } else {
        CorsLayer::new()
            .allow_origin(config.cors_origin.parse::<axum::http::HeaderValue>()?)
            .allow_methods([axum::http::Method::GET])
            .allow_headers([axum::http::header::CONTENT_TYPE, axum::http::header::ACCEPT])
    };

    let app = api::router(state.clone())
        .layer(cors)
        .layer(TraceLayer::new_for_http());

    let (shutdown_tx, shutdown_rx) = watch::channel(false);

    let bg_handles = if config.solana_rpc_url.is_empty() {
        warn!("SOLANA_RPC_URL not set — ingester and tip tracker disabled");
        Vec::new()
    } else {
        let rpc = RpcClient::new(config.solana_rpc_url.clone(), config.rpc_min_interval);

        let ingest_handle = {
            let store = state.store.clone();
            let rpc = rpc.clone();
            let cfg = IngestConfig {
                batch_size: config.ingest_batch_size,
                flush_interval: config.ingest_flush,
            };
            let rx = shutdown_rx.clone();
            tokio::spawn(async move {
                if let Err(e) = ingest::run(rpc, store, cfg, rx).await {
                    error!(error = %e, "ingester exited with error");
                }
            })
        };

        let tip_handle = {
            let tracker = state.tip.clone();
            let rx = shutdown_rx.clone();
            tokio::spawn(tracker.run(rpc, rx))
        };

        vec![ingest_handle, tip_handle]
    };

    let addr = SocketAddr::from(([0, 0, 0, 0], config.port));
    let listener = tokio::net::TcpListener::bind(addr).await?;
    info!(%addr, "server started");

    let server_shutdown = async move {
        let _ = tokio::signal::ctrl_c().await;
        info!("ctrl_c received, shutting down");
    };
    axum::serve(listener, app)
        .with_graceful_shutdown(server_shutdown)
        .await?;

    let _ = shutdown_tx.send(true);
    for handle in bg_handles {
        let _ = handle.await;
    }

    Ok(())
}

fn init_tracing() {
    let env_filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| "info".into());
    if std::env::var("LOG_FORMAT").as_deref() == Ok("json") {
        tracing_subscriber::fmt()
            .json()
            .with_env_filter(env_filter)
            .init();
    } else {
        tracing_subscriber::fmt().with_env_filter(env_filter).init();
    }
}
