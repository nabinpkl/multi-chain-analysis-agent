use std::sync::Arc;
use std::time::Duration;

use futures_util::StreamExt;
use parking_lot::RwLock;
use rdkafka::{Message, Offset, TopicPartitionList};
use rdkafka::consumer::{CommitMode, Consumer, StreamConsumer};
use tokio::sync::{broadcast, watch};
use tokio::time::{Instant, sleep};
use tracing::{debug, error, info, warn};

use crate::domain::Edge;
use crate::layout::{self, PositionStore};
use crate::state_machine::{StateMachine, Transition};
use crate::stream::topics::Envelope;

const COMMIT_EVERY_N: u64 = 1000;
const COMMIT_EVERY: Duration = Duration::from_secs(2);

pub async fn run(
    consumer: StreamConsumer,
    state: Arc<RwLock<StateMachine>>,
    raw_tx: broadcast::Sender<Arc<Edge>>,
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
                                let edge = Arc::new(env.edge);
                                state.write().apply(Transition::Increment((*edge).clone()));
                                // Fire-and-forget to raw subscribers. `send` errors only when
                                // there are no active receivers, which is the common case when
                                // no browsers are connected  not an error.
                                let _ = raw_tx.send(Arc::clone(&edge));
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

/// 1 Hz timer that:
///   1. Advances the sliding-window eviction in the state machine.
///   2. Takes a hub-view snapshot over the max window and hands it to
///      the tiled layout, which recomputes `PositionStore` from
///      scratch as a pure function of the current (nodes, edges).
///   3. Broadcasts so every SSE subscriber re-snapshots for its own
///      window. We broadcast unconditionally because a topology
///      change can move tiles and the frontend needs to tween.
pub async fn tick_loop(
    state: Arc<RwLock<StateMachine>>,
    positions: Arc<RwLock<PositionStore>>,
    tx: broadcast::Sender<()>,
    window_secs: u32,
    interval: Duration,
    mut shutdown_rx: watch::Receiver<bool>,
) {
    loop {
        tokio::select! {
            _ = shutdown_rx.changed() => {
                info!("state-tick: shutdown received");
                return;
            }
            _ = sleep(interval) => {
                let now_u32 = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs() as u32)
                    .unwrap_or(0);

                // 1. Advance window eviction.
                state.write().apply(Transition::AdvanceWindow(now_u32));

                // 2. Pull the current hub-view subgraph and recompute
                //    the tiled layout against it. Separate locks, so
                //    this doesn't block SSE readers who only need
                //    state_machine.
                let (nodes, edges) = {
                    let snap = state.read().snapshot_window(now_u32, window_secs);
                    (snap.nodes, snap.edges)
                };
                {
                    let mut store = positions.write();
                    layout::advance(
                        &mut store,
                        &nodes,
                        &edges,
                        std::time::Instant::now(),
                    );
                }

                // 3. Signal subscribers. Positions may have drifted even
                //    if aggregates didn't, so we always broadcast.
                let _ = tx.send(());
            }
        }
    }
}
