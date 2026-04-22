use std::sync::Arc;
use std::time::Duration;

use futures_util::StreamExt;
use parking_lot::RwLock;
use rdkafka::{Message, Offset, TopicPartitionList};
use rdkafka::consumer::{CommitMode, Consumer, StreamConsumer};
use tokio::sync::{broadcast, watch};
use tokio::time::{Instant, sleep};
use tracing::{debug, error, info, warn};

use crate::state_machine::{StateMachine, Transition};
use crate::stream::topics::Envelope;

const COMMIT_EVERY_N: u64 = 1000;
const COMMIT_EVERY: Duration = Duration::from_secs(2);

pub async fn run(
    consumer: StreamConsumer,
    state: Arc<RwLock<StateMachine>>,
    mut shutdown_rx: watch::Receiver<bool>,
) -> anyhow::Result<()> {
    let mut stream = consumer.stream();
    let mut since_commit: u64 = 0;
    let mut last_commit = Instant::now();
    let mut last_tpl: Option<(String, i32, i64)> = None;

    loop {
        tokio::select! {
            _ = shutdown_rx.changed() => {
                info!("state-sink: shutdown received, committing");
                commit(&consumer, &last_tpl, CommitMode::Sync);
                return Ok(());
            }
            maybe = stream.next() => {
                match maybe {
                    Some(Ok(msg)) => {
                        let payload = match msg.payload() {
                            Some(p) => p,
                            None => {
                                warn!("state-sink: empty payload, skipping");
                                continue;
                            }
                        };
                        match serde_json::from_slice::<Envelope>(payload) {
                            Ok(env) => {
                                state.write().apply(Transition::Increment(env.edge));
                                last_tpl = Some((msg.topic().to_string(), msg.partition(), msg.offset()));
                                since_commit += 1;
                                if since_commit >= COMMIT_EVERY_N || last_commit.elapsed() >= COMMIT_EVERY {
                                    commit(&consumer, &last_tpl, CommitMode::Async);
                                    since_commit = 0;
                                    last_commit = Instant::now();
                                }
                            }
                            Err(e) => {
                                warn!(error = %e, "state-sink: envelope parse failed, skipping");
                            }
                        }
                    }
                    Some(Err(e)) => {
                        error!(error = %e, "state-sink: consumer error");
                        sleep(Duration::from_millis(500)).await;
                    }
                    None => {
                        info!("state-sink: stream ended");
                        commit(&consumer, &last_tpl, CommitMode::Sync);
                        return Ok(());
                    }
                }
            }
        }
    }
}

fn commit(consumer: &StreamConsumer, last: &Option<(String, i32, i64)>, mode: CommitMode) {
    let Some((topic, partition, offset)) = last else {
        return;
    };
    let mut tpl = TopicPartitionList::new();
    if let Err(e) = tpl.add_partition_offset(topic, *partition, Offset::Offset(*offset + 1)) {
        warn!(error = %e, "state-sink: tpl add failed");
        return;
    }
    if let Err(e) = consumer.commit(&tpl, mode) {
        warn!(error = %e, "state-sink: commit failed");
    } else {
        debug!(offset = *offset + 1, "state-sink: committed");
    }
}

/// 1Hz timer that drives the sliding-window eviction transition AND
/// signals SSE subscribers that the state has changed. Each connection
/// re-snapshots for its own window on signal — no snapshots on the wire.
pub async fn tick_loop(
    state: Arc<RwLock<StateMachine>>,
    tx: broadcast::Sender<()>,
    interval: Duration,
    mut shutdown_rx: watch::Receiver<bool>,
) {
    let mut last_seq: u64 = 0;
    loop {
        tokio::select! {
            _ = shutdown_rx.changed() => {
                info!("state-tick: shutdown received");
                return;
            }
            _ = sleep(interval) => {
                let now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs() as u32)
                    .unwrap_or(0);

                let changed = {
                    let mut sm = state.write();
                    sm.apply(Transition::AdvanceWindow(now));
                    let seq = sm.seq();
                    if seq == last_seq {
                        false
                    } else {
                        last_seq = seq;
                        true
                    }
                };

                if changed {
                    let _ = tx.send(());
                }
            }
        }
    }
}
