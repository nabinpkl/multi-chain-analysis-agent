use std::sync::Arc;
use std::time::Duration;

use tokio::sync::mpsc;
use tokio::sync::watch;
use tokio::time::{Instant, sleep};
use tracing::{debug, error, info, warn};

use crate::domain::Edge;
use crate::rpc::{RpcClient, RpcError};
use crate::store::EdgeStore;

const COMPONENT: &str = "solana_ingester";
const CHANNEL_CAPACITY: usize = 100;
const NOT_AVAILABLE_DELAY_MS: u64 = 400;
const RATE_LIMIT_INITIAL_MS: u64 = 500;
const RATE_LIMIT_MAX_MS: u64 = 8_000;
const TRANSIENT_INITIAL_MS: u64 = 500;
const TRANSIENT_MAX_RETRIES: u32 = 3;
const TRANSIENT_LONG_SLEEP_MS: u64 = 5_000;
const FLUSH_MAX_ATTEMPTS: u32 = 3;
const FLUSH_INITIAL_BACKOFF_MS: u64 = 1_000;

pub struct IngestConfig {
    pub batch_size: usize,
    pub flush_interval: Duration,
}

pub async fn run(
    rpc: RpcClient,
    store: Arc<dyn EdgeStore>,
    cfg: IngestConfig,
    mut shutdown_rx: watch::Receiver<bool>,
) -> anyhow::Result<()> {
    let starting_slot = match store.get_last_slot(COMPONENT).await? {
        Some(s) => s + 1,
        None => {
            let tip = rpc.get_slot().await?;
            info!(tip, "no checkpoint, starting from current tip");
            tip
        }
    };
    info!(slot = starting_slot, "ingester starting");

    let (tx, rx) = mpsc::channel::<(u64, Vec<Edge>)>(CHANNEL_CAPACITY);

    let writer_store = store.clone();
    let writer_shutdown = shutdown_rx.clone();
    let writer_handle =
        tokio::spawn(async move { writer_loop(writer_store, rx, cfg, writer_shutdown).await });

    fetcher_loop(rpc, tx, starting_slot, &mut shutdown_rx).await;

    if let Err(e) = writer_handle.await? {
        error!(error = %e, "writer task ended with error");
    }
    Ok(())
}

async fn fetcher_loop(
    rpc: RpcClient,
    tx: mpsc::Sender<(u64, Vec<Edge>)>,
    mut next: u64,
    shutdown_rx: &mut watch::Receiver<bool>,
) {
    let mut rate_backoff_ms = RATE_LIMIT_INITIAL_MS;

    loop {
        if *shutdown_rx.borrow() {
            info!("fetcher: shutdown received");
            return;
        }

        let result = tokio::select! {
            _ = shutdown_rx.changed() => {
                info!("fetcher: shutdown received mid-fetch");
                return;
            }
            r = rpc.get_block(next) => r,
        };

        match result {
            Ok(block) => {
                let version = epoch_ms();
                let edges = crate::ingest::parser::parse_edges(&block, next, version);
                debug!(slot = next, edges = edges.len(), "fetched block");
                if tx.send((next, edges)).await.is_err() {
                    warn!("writer dropped, exiting fetcher");
                    return;
                }
                next += 1;
                rate_backoff_ms = RATE_LIMIT_INITIAL_MS;
            }
            Err(RpcError::SkippedSlot) => {
                if tx.send((next, Vec::new())).await.is_err() {
                    return;
                }
                next += 1;
            }
            Err(RpcError::NotYetAvailable) => {
                sleep(Duration::from_millis(NOT_AVAILABLE_DELAY_MS)).await;
            }
            Err(RpcError::RateLimited) => {
                warn!(backoff_ms = rate_backoff_ms, "rate limited");
                sleep(Duration::from_millis(rate_backoff_ms)).await;
                rate_backoff_ms = (rate_backoff_ms * 2).min(RATE_LIMIT_MAX_MS);
            }
            Err(RpcError::Transient(msg)) => {
                if !retry_transient(&rpc, next, &msg).await {
                    sleep(Duration::from_millis(TRANSIENT_LONG_SLEEP_MS)).await;
                }
            }
            Err(RpcError::Fatal(msg)) => {
                error!(slot = next, error = %msg, "fatal block error, skipping slot");
                if tx.send((next, Vec::new())).await.is_err() {
                    return;
                }
                next += 1;
            }
        }
    }
}

async fn retry_transient(rpc: &RpcClient, slot: u64, original: &str) -> bool {
    let mut delay = TRANSIENT_INITIAL_MS;
    for attempt in 1..=TRANSIENT_MAX_RETRIES {
        warn!(slot, attempt, error = %original, "transient error, retrying");
        sleep(Duration::from_millis(delay)).await;
        match rpc.get_block(slot).await {
            Ok(_) => return true,
            Err(RpcError::Transient(_)) => delay = (delay * 2).min(8_000),
            Err(_) => return true,
        }
    }
    false
}

async fn writer_loop(
    store: Arc<dyn EdgeStore>,
    mut rx: mpsc::Receiver<(u64, Vec<Edge>)>,
    cfg: IngestConfig,
    mut shutdown_rx: watch::Receiver<bool>,
) -> anyhow::Result<()> {
    let mut buf: Vec<Edge> = Vec::with_capacity(cfg.batch_size);
    let mut highest_slot: Option<u64> = None;
    let mut last_flush = Instant::now();

    loop {
        let until_flush = cfg
            .flush_interval
            .saturating_sub(last_flush.elapsed());
        let timeout = if until_flush.is_zero() {
            Duration::from_millis(1)
        } else {
            until_flush
        };

        tokio::select! {
            _ = shutdown_rx.changed() => {
                info!("writer: shutdown received, draining");
                while let Ok((slot, edges)) = rx.try_recv() {
                    buf.extend(edges);
                    highest_slot = Some(highest_slot.map_or(slot, |h| h.max(slot)));
                }
                flush(&store, &mut buf, &mut highest_slot).await;
                return Ok(());
            }
            maybe = rx.recv() => {
                match maybe {
                    Some((slot, edges)) => {
                        buf.extend(edges);
                        highest_slot = Some(highest_slot.map_or(slot, |h| h.max(slot)));
                        if buf.len() >= cfg.batch_size {
                            flush(&store, &mut buf, &mut highest_slot).await;
                            last_flush = Instant::now();
                        }
                    }
                    None => {
                        flush(&store, &mut buf, &mut highest_slot).await;
                        return Ok(());
                    }
                }
            }
            _ = sleep(timeout) => {
                if !buf.is_empty() || highest_slot.is_some() {
                    flush(&store, &mut buf, &mut highest_slot).await;
                }
                last_flush = Instant::now();
            }
        }
    }
}

async fn flush(
    store: &Arc<dyn EdgeStore>,
    buf: &mut Vec<Edge>,
    highest_slot: &mut Option<u64>,
) {
    if buf.is_empty() && highest_slot.is_none() {
        return;
    }
    let edge_count = buf.len();
    let mut backoff = FLUSH_INITIAL_BACKOFF_MS;

    for attempt in 1..=FLUSH_MAX_ATTEMPTS {
        let result: anyhow::Result<()> = async {
            if !buf.is_empty() {
                store.insert_edges(buf).await?;
            }
            if let Some(slot) = *highest_slot {
                store.set_last_slot(COMPONENT, slot).await?;
            }
            Ok(())
        }
        .await;

        match result {
            Ok(()) => {
                info!(
                    edges = edge_count,
                    slot = highest_slot.unwrap_or(0),
                    attempt,
                    "flushed"
                );
                buf.clear();
                *highest_slot = None;
                return;
            }
            Err(e) if attempt < FLUSH_MAX_ATTEMPTS => {
                error!(
                    error = %e,
                    attempt,
                    backoff_ms = backoff,
                    "flush failed, retrying"
                );
                sleep(Duration::from_millis(backoff)).await;
                backoff *= 2;
            }
            Err(e) => {
                error!(
                    error = %e,
                    attempt,
                    "flush failed permanently after retries, exiting for container restart"
                );
                std::process::exit(1);
            }
        }
    }
}

fn epoch_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}
