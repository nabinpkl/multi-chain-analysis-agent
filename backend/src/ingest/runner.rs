use std::sync::Arc;
use std::time::Duration;

use tokio::sync::watch;
use tokio::time::{Instant, sleep};
use tracing::{error, info, warn};

use crate::rpc::{RpcClient, RpcError};
use crate::store::EdgeStore;
use crate::stream::EdgeProducer;
use crate::tip::TipTracker;

const COMPONENT: &str = "solana_ingester";
const SLOT_PRODUCTION_MS: u64 = 400;
const RATE_LIMIT_INITIAL_MS: u64 = 500;
const RATE_LIMIT_MAX_MS: u64 = 8_000;
const TRANSIENT_INITIAL_MS: u64 = 500;
const TRANSIENT_MAX_RETRIES: u32 = 3;
const TRANSIENT_LONG_SLEEP_MS: u64 = 5_000;
const CHECKPOINT_EVERY_SLOTS: u64 = 16;
const PROGRESS_LOG_INTERVAL: Duration = Duration::from_secs(30);

pub async fn run(
    rpc: RpcClient,
    store: Arc<dyn EdgeStore>,
    producer: EdgeProducer,
    tip: TipTracker,
    mut shutdown_rx: watch::Receiver<bool>,
) -> anyhow::Result<()> {
    let starting_slot = match store.get_last_slot(COMPONENT).await? {
        Some(s) => s + 1,
        None => {
            let t = rpc.get_slot().await?;
            info!(tip = t, "no checkpoint, starting from current tip");
            t
        }
    };
    info!(slot = starting_slot, "ingester starting");

    fetcher_loop(rpc, producer, store, tip, starting_slot, &mut shutdown_rx).await;
    Ok(())
}

#[derive(Default)]
struct ProgressCounters {
    slots: u64,
    edges: u64,
    rate_limits: u64,
    transient_errors: u64,
    skipped: u64,
}

async fn fetcher_loop(
    rpc: RpcClient,
    producer: EdgeProducer,
    store: Arc<dyn EdgeStore>,
    tip: TipTracker,
    mut next: u64,
    shutdown_rx: &mut watch::Receiver<bool>,
) {
    let mut rate_backoff_ms = RATE_LIMIT_INITIAL_MS;
    let mut slots_since_checkpoint: u64 = 0;
    let mut counters = ProgressCounters::default();
    let mut last_log = Instant::now();

    loop {
        if *shutdown_rx.borrow() {
            info!("fetcher: shutdown received");
            return;
        }

        // Pre-flight: skip getBlock if cached tip says the slot isn't produced yet.
        // Prevents burning RPC budget on guaranteed-NotYetAvailable calls.
        if let Some(tip_slot) = tip.current() {
            if next > tip_slot {
                let behind = next - tip_slot;
                let wait = Duration::from_millis(behind * SLOT_PRODUCTION_MS);
                tokio::select! {
                    _ = shutdown_rx.changed() => return,
                    _ = sleep(wait) => {}
                }
                continue;
            }
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
                info!(slot = next, edges = edges.len(), "ingested block");
                for edge in &edges {
                    if let Err(e) = producer.publish(edge).await {
                        warn!(slot = next, error = %e, "kafka publish failed; will retry block");
                        sleep(Duration::from_millis(500)).await;
                        continue;
                    }
                }
                counters.edges += edges.len() as u64;
                next += 1;
                counters.slots += 1;
                slots_since_checkpoint += 1;
                rate_backoff_ms = RATE_LIMIT_INITIAL_MS;

                if slots_since_checkpoint >= CHECKPOINT_EVERY_SLOTS {
                    if let Err(e) = store.set_last_slot(COMPONENT, next - 1).await {
                        warn!(error = %e, "checkpoint write failed");
                    }
                    slots_since_checkpoint = 0;
                }
            }
            Err(RpcError::SkippedSlot) => {
                next += 1;
                counters.slots += 1;
                counters.skipped += 1;
                slots_since_checkpoint += 1;
            }
            Err(RpcError::NotYetAvailable) => {
                // Cached tip was stale and we hit a slot the network hasn't produced.
                // Refresh tip once so subsequent iterations sleep instead of poking getBlock.
                match rpc.get_slot().await {
                    Ok(t) => {
                        tip.set(t);
                        let behind = next.saturating_sub(t).max(1);
                        sleep(Duration::from_millis(behind * SLOT_PRODUCTION_MS)).await;
                    }
                    Err(_) => sleep(Duration::from_millis(SLOT_PRODUCTION_MS)).await,
                }
            }
            Err(RpcError::RateLimited) => {
                counters.rate_limits += 1;
                warn!(backoff_ms = rate_backoff_ms, "rate limited");
                sleep(Duration::from_millis(rate_backoff_ms)).await;
                rate_backoff_ms = (rate_backoff_ms * 2).min(RATE_LIMIT_MAX_MS);
            }
            Err(RpcError::Transient(msg)) => {
                counters.transient_errors += 1;
                if !retry_transient(&rpc, next, &msg).await {
                    sleep(Duration::from_millis(TRANSIENT_LONG_SLEEP_MS)).await;
                }
            }
            Err(RpcError::Fatal(msg)) => {
                error!(slot = next, error = %msg, "fatal block error, skipping slot");
                next += 1;
                counters.slots += 1;
                counters.skipped += 1;
                slots_since_checkpoint += 1;
            }
        }

        if last_log.elapsed() >= PROGRESS_LOG_INTERVAL {
            let elapsed_secs = last_log.elapsed().as_secs_f32().max(0.001);
            let slots_per_s = counters.slots as f32 / elapsed_secs;
            let tip_slot = tip.current();
            let drift = tip_slot.map(|t| t.saturating_sub(next));
            info!(
                at_slot = next,
                tip = ?tip_slot,
                drift_slots = ?drift,
                slots_per_s = format!("{:.2}", slots_per_s),
                edges = counters.edges,
                skipped_slots = counters.skipped,
                rate_limits = counters.rate_limits,
                transient_errors = counters.transient_errors,
                "ingester progress"
            );
            counters = ProgressCounters::default();
            last_log = Instant::now();
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

fn epoch_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}
