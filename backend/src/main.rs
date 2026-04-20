use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Instant;

use axum::Router;
use axum::routing::get;
use tower_http::cors::CorsLayer;
use tracing::{info, warn};


#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    // CORS
    let cors_origin = std::env::var("CORS_ORIGIN").unwrap_or_else(|_| "*".to_string());
    let cors = if cors_origin == "*" {
        info!("CORS: allowing all origins (development mode)");
        CorsLayer::permissive()
    } else {
        info!(origin = %cors_origin, "CORS: restricting to origin");
        CorsLayer::new()
            .allow_origin(
                cors_origin
                    .parse::<axum::http::HeaderValue>()
                    .expect("invalid CORS_ORIGIN"),
            )
            .allow_methods([axum::http::Method::GET])
            .allow_headers([axum::http::header::CONTENT_TYPE, axum::http::header::ACCEPT])
    };

    let app = Router::new()
        .route("/health", get(handlers::health_handler))
        .route("/ready", get(handlers::ready_handler))
        .route("/search", get(handlers::search_handler))
        .route("/stream", get(handlers::stream_handler))
        .route("/tlds", get(handlers::tlds_handler))
        .route("/tlds-for", get(handlers::tlds_for_handler))
        .route("/categories", get(handlers::categories_handler))
        .route("/stats", get(handlers::stats_handler))
        .layer(cors)
        .with_state(state.clone());

    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(3001);
    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("failed to bind port");
    info!(%addr, "server started");

    // Scheduler runs in background. It will kick off the first batch
    // immediately if no snapshot was loaded.
    let scheduler_state = state.clone();
    tokio::spawn(async move {
        scheduler::run(scheduler_state).await;
    });

    axum::serve(listener, app).await.unwrap();
}
