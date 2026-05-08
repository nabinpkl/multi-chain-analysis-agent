use std::net::SocketAddr;

use tokio::sync::watch;
use tower_http::cors::CorsLayer;
use tower_http::trace::TraceLayer;
use tracing::{error, info, warn};

// Modules live in the library crate so adjacent bins can reach them.
// The server binary just imports.
use multichain_engine::{
    analytics, api, config, graph, ingest, rpc, sinks, snapshot, state, store, stream,
};

use config::Config;
use rpc::RpcClient;
use sinks::stream_sink::{self, SinkConfig};
use state::AppState;
use stream::{
    EdgeProducer, EdgeStream, MetadataProducer, MetadataStream, consumer::build_consumer,
};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    init_tracing();

    let config = Config::from_env();
    info!(?config.clickhouse_url, ?config.clickhouse_db, "loaded config");

    let (state, analytics_senders) = AppState::new(&config);

    store::schema::bootstrap(&state.clickhouse).await?;
    info!("clickhouse schema bootstrapped");

    // Spawn the snapshot-cache GC sweep so leased turn snapshots not
    // released within 5 min get dropped instead of leaking. Cancelled
    // at runtime exit.
    snapshot::spawn_gc(state.snapshot_cache.clone());
    info!("snapshot cache gc spawned");

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

    // edge-sink: persists edges to ClickHouse. Separate consumer
    // group. Uses the generic `stream_sink::run` parameterized over
    // `EdgeStream`; future stream types spawn their own task with the
    // same body, parameterized over their own `IngestStream` impl.
    let edge_sink_handle = {
        let consumer = build_consumer(
            &config.kafka_brokers,
            &config.kafka_group_ch_sink,
            &config.kafka_topic_raw_edges,
            &config.kafka_auto_offset_reset,
        )?;
        info!(group = %config.kafka_group_ch_sink, topic = %config.kafka_topic_raw_edges, "edge-sink consumer ready");
        let store = state.store.clone();
        let cfg = SinkConfig {
            batch_size: config.ch_sink_batch_size,
            flush_interval: config.ch_sink_flush,
        };
        let rx = shutdown_rx.clone();
        tokio::spawn(async move {
            if let Err(e) = stream_sink::run::<EdgeStream>(consumer, store, cfg, rx).await {
                error!(error = %e, "edge-sink exited with error");
            }
        })
    };
    bg_handles.push(edge_sink_handle);

    // token-metadata-sink: persists Metaplex (and future Token-2022)
    // metadata writes to ClickHouse. Parallel task to edge-sink against
    // the metadata topic, isolated so a metadata-side stall does not
    // block edge ingestion.
    let metadata_sink_handle = {
        let consumer = build_consumer(
            &config.kafka_brokers,
            "token-metadata-sink",
            &config.kafka_topic_token_metadata,
            &config.kafka_auto_offset_reset,
        )?;
        info!(group = "token-metadata-sink", topic = %config.kafka_topic_token_metadata, "token-metadata-sink consumer ready");
        let store = state.store.clone();
        let cfg = SinkConfig {
            batch_size: config.ch_sink_batch_size,
            flush_interval: config.ch_sink_flush,
        };
        let rx = shutdown_rx.clone();
        tokio::spawn(async move {
            if let Err(e) = stream_sink::run::<MetadataStream>(consumer, store, cfg, rx).await {
                error!(error = %e, "token-metadata-sink exited with error");
            }
        })
    };
    bg_handles.push(metadata_sink_handle);

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

    // analytics: per-window community-detection tasks. One task per
    // rolling window, each on a 3s tick. Snapshot pattern: brief read
    // lock on `GraphState`, copy the per-window edge view, release,
    // run Louvain off-lock, broadcast the diff.
    {
        let analytics_handles =
            analytics::spawn_all(state.clone(), analytics_senders, shutdown_rx.clone());
        info!(
            count = analytics_handles.len(),
            "analytics tasks spawned (one per window)"
        );
        for h in analytics_handles {
            bg_handles.push(h);
        }
    }

    if config.solana_rpc_url.is_empty() {
        warn!("SOLANA_RPC_URL not set  ingester and tip tracker disabled");
    } else {
        let rpc = RpcClient::new(config.solana_rpc_url.clone(), config.rpc_min_interval);

        let edge_producer = EdgeProducer::new(&config.kafka_brokers, &config.kafka_topic_raw_edges)?;
        info!(brokers = %config.kafka_brokers, topic = %config.kafka_topic_raw_edges, "kafka edge producer ready");

        let metadata_producer = MetadataProducer::new(
            &config.kafka_brokers,
            &config.kafka_topic_token_metadata,
        )?;
        info!(
            brokers = %config.kafka_brokers,
            topic = %config.kafka_topic_token_metadata,
            "kafka token-metadata producer ready"
        );

        let ingest_handle = {
            let store = state.store.clone();
            let rpc = rpc.clone();
            let tip = state.tip.clone();
            let rx = shutdown_rx.clone();
            tokio::spawn(async move {
                if let Err(e) = ingest::run(rpc, store, edge_producer, metadata_producer, tip, rx)
                    .await
                {
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
