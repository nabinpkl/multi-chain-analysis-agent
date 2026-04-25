use std::net::SocketAddr;

use tokio::sync::watch;
use tower_http::cors::CorsLayer;
use tower_http::trace::TraceLayer;
use tracing::{error, info, warn};

mod api;
mod config;
mod domain;
mod ingest;
mod layout;
mod rpc;
mod sinks;
mod state;
mod state_machine;
mod store;
mod stream;
mod tip;

use config::Config;
use rpc::RpcClient;
use sinks::ch_sink::{self, ChSinkConfig};
use sinks::state_sink;
use state::AppState;
use stream::{EdgeProducer, consumer::build_consumer};

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

    let mut bg_handles = Vec::new();

    // state-sink consumer  drives the in-memory projection from Kafka.
    let state_sink_handle = {
        let consumer = build_consumer(
            &config.kafka_brokers,
            &config.kafka_group_live_state,
            &config.kafka_topic_raw_edges,
            &config.kafka_auto_offset_reset,
        )?;
        info!(group = %config.kafka_group_live_state, topic = %config.kafka_topic_raw_edges, "state-sink consumer ready");
        let sm = state.state_machine.clone();
        let raw_tx = state.raw_tx.clone();
        let rx = shutdown_rx.clone();
        tokio::spawn(async move {
            if let Err(e) = state_sink::run(consumer, sm, raw_tx, rx).await {
                error!(error = %e, "state-sink exited with error");
            }
        })
    };
    bg_handles.push(state_sink_handle);

    // 1Hz tick: AdvanceWindow + advance the force layout + SSE signal.
    let tick_handle = {
        let sm = state.state_machine.clone();
        let positions = state.positions.clone();
        let tx = state.tick_tx.clone();
        let window_secs = state.window_secs;
        let interval = config.state_tick_interval;
        let rx = shutdown_rx.clone();
        tokio::spawn(state_sink::tick_loop(
            sm,
            positions,
            tx,
            window_secs,
            interval,
            rx,
        ))
    };
    bg_handles.push(tick_handle);

    // ch-sink consumer  accumulates cold projection.
    let ch_sink_handle = {
        let consumer = build_consumer(
            &config.kafka_brokers,
            &config.kafka_group_ch_sink,
            &config.kafka_topic_raw_edges,
            &config.kafka_auto_offset_reset,
        )?;
        info!(group = %config.kafka_group_ch_sink, topic = %config.kafka_topic_raw_edges, "ch-sink consumer ready");
        let store = state.store.clone();
        let cfg = ChSinkConfig {
            batch_size: config.ch_sink_batch_size,
            flush_interval: config.ch_sink_flush,
        };
        let rx = shutdown_rx.clone();
        tokio::spawn(async move {
            if let Err(e) = ch_sink::run(consumer, store, cfg, rx).await {
                error!(error = %e, "ch-sink exited with error");
            }
        })
    };
    bg_handles.push(ch_sink_handle);

    if config.solana_rpc_url.is_empty() {
        warn!("SOLANA_RPC_URL not set  ingester and tip tracker disabled");
    } else {
        let rpc = RpcClient::new(config.solana_rpc_url.clone(), config.rpc_min_interval);

        let producer = EdgeProducer::new(&config.kafka_brokers, &config.kafka_topic_raw_edges)?;
        info!(brokers = %config.kafka_brokers, topic = %config.kafka_topic_raw_edges, "kafka producer ready");

        let ingest_handle = {
            let store = state.store.clone();
            let rpc = rpc.clone();
            let tip = state.tip.clone();
            let rx = shutdown_rx.clone();
            tokio::spawn(async move {
                if let Err(e) = ingest::run(rpc, store, producer, tip, rx).await {
                    error!(error = %e, "ingester exited with error");
                }
            })
        };
        bg_handles.push(ingest_handle);

        let tip_handle = {
            let tracker = state.tip.clone();
            let rx = shutdown_rx.clone();
            tokio::spawn(tracker.run(rpc, rx))
        };
        bg_handles.push(tip_handle);
    }

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
