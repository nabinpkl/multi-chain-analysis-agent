use std::sync::Arc;
use std::time::Duration;

use futures_util::StreamExt;
use parking_lot::RwLock;
use rdkafka::{Message, Offset, TopicPartitionList};
use rdkafka::consumer::{CommitMode, Consumer, StreamConsumer};
use tokio::sync::watch;
use tokio::time::{Instant, sleep};
use tracing::{debug, error, info, warn};

use crate::state::WindowChannels;
use crate::stream::topics::Envelope;
use super::GraphState;

const COMMIT_EVERY_N: u64 = 1000;
const COMMIT_EVERY: Duration = Duration::from_secs(2);

pub async fn run(
    consumer: StreamConsumer,
    graph: Arc<RwLock<GraphState>>,
    channels: WindowChannels,
    mut shutdown: watch::Receiver<bool>,
) -> anyhow::Result<()> {
    let mut stream = consumer.stream();
    let mut since_commit: u64 = 0;
    let mut last_commit = Instant::now();
    let mut last_tpl: Option<(String, i32, i64)> = None;

    info!("graph-consumer: started");

    loop {
        tokio::select! {
            _ = shutdown.changed() => {
                info!("graph-consumer: shutdown received, committing");
                commit(&consumer, &last_tpl, CommitMode::Sync);
                return Ok(());
            }
            maybe = stream.next() => {
                match maybe {
                    Some(Ok(msg)) => {
                        let payload = match msg.payload() {
                            Some(p) => p,
                            None => {
                                warn!("graph-consumer: empty payload, skipping");
                                continue;
                            }
                        };
                        match serde_json::from_slice::<Envelope>(payload) {
                            Ok(env) => {
                                let ingest = {
                                    let mut g = graph.write();
                                    g.ingest(&env.edge)
                                };
                                if !ingest.is_empty() {
                                    dispatch(&channels, ingest);
                                }
                                last_tpl = Some((
                                    msg.topic().to_string(),
                                    msg.partition(),
                                    msg.offset(),
                                ));
                                since_commit += 1;
                                if since_commit >= COMMIT_EVERY_N
                                    || last_commit.elapsed() >= COMMIT_EVERY
                                {
                                    commit(&consumer, &last_tpl, CommitMode::Async);
                                    since_commit = 0;
                                    last_commit = Instant::now();
                                }
                            }
                            Err(e) => {
                                warn!(error = %e, "graph-consumer: envelope parse failed, skipping");
                            }
                        }
                    }
                    Some(Err(e)) => {
                        error!(error = %e, "graph-consumer: consumer error");
                        sleep(Duration::from_millis(500)).await;
                    }
                    None => {
                        info!("graph-consumer: stream ended");
                        commit(&consumer, &last_tpl, CommitMode::Sync);
                        return Ok(());
                    }
                }
            }
        }
    }
}

/// Fan ingest output into per-window broadcast channels. `common` events
/// go to every channel; `per_window[w]` only to channel `w`. Each window
/// receives at most one Arc<Vec<GraphDelta>> per ingest call, preserving
/// chronological order within that window's stream.
fn dispatch(channels: &WindowChannels, ingest: super::IngestDeltas) {
    let common = ingest.common;
    let common_arc = if common.is_empty() {
        None
    } else {
        Some(Arc::new(common))
    };

    let mut per_window = ingest.per_window;
    for (w, tx) in channels.txs.iter().enumerate() {
        let mut batch: Vec<crate::graph::delta::GraphDelta> = Vec::new();
        if let Some(c) = &common_arc {
            batch.extend_from_slice(c);
        }
        let win_specific = std::mem::take(&mut per_window[w]);
        batch.extend(win_specific);
        if batch.is_empty() {
            continue;
        }
        let _ = tx.send(Arc::new(batch));
    }
}

fn commit(consumer: &StreamConsumer, last: &Option<(String, i32, i64)>, mode: CommitMode) {
    let Some((topic, partition, offset)) = last else {
        return;
    };
    let mut tpl = TopicPartitionList::new();
    if let Err(e) = tpl.add_partition_offset(topic, *partition, Offset::Offset(*offset + 1)) {
        warn!(error = %e, "graph-consumer: tpl add failed");
        return;
    }
    if let Err(e) = consumer.commit(&tpl, mode) {
        warn!(error = %e, "graph-consumer: commit failed");
    } else {
        debug!(offset = *offset + 1, "graph-consumer: committed");
    }
}
