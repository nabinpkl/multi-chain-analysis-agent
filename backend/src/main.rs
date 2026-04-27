use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use parking_lot::RwLock;
use tokio::sync::watch;
use tower_http::cors::CorsLayer;
use tower_http::trace::TraceLayer;
use tracing::{debug, error, info, warn};

use crate::graph::GraphState;
use crate::graph::delta::GraphDelta;
use crate::state::WindowChannels;

mod api;
mod config;
mod domain;
mod graph;
mod ingest;
mod rpc;
mod sinks;
mod state;
mod store;
mod stream;
mod tip;

use config::Config;
use rpc::RpcClient;
use sinks::ch_sink::{self, ChSinkConfig};
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

    // ch-sink: persists edges to ClickHouse. Separate consumer group.
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

    // graph-engine: sole live ingest path. Builds in-memory graph and
    // dispatches GraphDelta batches per-window to /graph/stream
    // subscribers.
    let graph_consumer_handle = {
        let consumer = build_consumer(
            &config.kafka_brokers,
            &config.kafka_group_graph,
            &config.kafka_topic_raw_edges,
            "latest",
        )?;
        info!(group = %config.kafka_group_graph, topic = %config.kafka_topic_raw_edges, "graph-engine consumer ready");
        let graph = state.graph.clone();
        let channels = state.deltas.clone();
        let rx = shutdown_rx.clone();
        tokio::spawn(async move {
            if let Err(e) = graph::consumer::run(consumer, graph, channels, rx).await {
                error!(error = %e, "graph-consumer exited with error");
            }
        })
    };
    bg_handles.push(graph_consumer_handle);

    // layout-tick: 60 Hz physics step. Broadcasts PositionsBatch to
    // every window channel since positions are window-agnostic.
    let layout_tick_handle = {
        let graph = state.graph.clone();
        let channels = state.deltas.clone();
        let rx = shutdown_rx.clone();
        tokio::spawn(layout_tick_loop(graph, channels, rx))
    };
    bg_handles.push(layout_tick_handle);

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

/// 30 Hz physics step. Acquires the graph write lock, calls `step_layout`,
/// broadcasts the resulting position diff. Empty diffs (graph at
/// equilibrium) are skipped so subscribers don't see noise events.
async fn layout_tick_loop(
    graph: Arc<RwLock<GraphState>>,
    channels: WindowChannels,
    mut shutdown: watch::Receiver<bool>,
) {
    // 60 Hz to match the frontend's RAF cadence.
    let mut ticker = tokio::time::interval(Duration::from_millis(16));
    info!("layout-tick: started (60 Hz)");
    loop {
        tokio::select! {
            _ = shutdown.changed() => {
                info!("layout-tick: shutdown received");
                return;
            }
            _ = ticker.tick() => {
                let maybe_delta = {
                    let mut g = graph.write();
                    let positions = g.step_layout();
                    if positions.is_empty() {
                        None
                    } else {
                        let seq = g.alloc_seq();
                        Some(GraphDelta::PositionsBatch { seq, positions })
                    }
                };
                let Some(delta) = maybe_delta else { continue };
                let batch = Arc::new(vec![delta]);
                let any_alive = channels.txs.iter().any(|tx| tx.receiver_count() > 0);
                if !any_alive {
                    debug!("layout-tick: no SSE subscribers");
                    continue;
                }
                channels.broadcast_all(batch);
            }
        }
    }
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
