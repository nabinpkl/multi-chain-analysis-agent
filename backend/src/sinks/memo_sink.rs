use std::sync::Arc;
use std::time::Duration;

use futures_util::StreamExt;
use rdkafka::consumer::{CommitMode, Consumer, StreamConsumer};
use rdkafka::{Message, Offset, TopicPartitionList};
use tokio::sync::watch;
use tokio::time::{Instant, sleep};
use tracing::{debug, error, info, warn};

use crate::domain::Memo;
use crate::store::EdgeStore;
use crate::stream::topics::MemoEnvelope;

/// Mirror of `ch_sink::run` for the memo topic. Same batching + commit
/// pattern; isolating it as its own task so a memo-side failure
/// doesn't stall edge ingestion. Reuses `EdgeStore::insert_memos` for
/// the actual ClickHouse write.
pub struct MemoSinkConfig {
    pub batch_size: usize,
    pub flush_interval: Duration,
}

pub async fn run(
    consumer: StreamConsumer,
    store: Arc<dyn EdgeStore>,
    cfg: MemoSinkConfig,
    mut shutdown_rx: watch::Receiver<bool>,
) -> anyhow::Result<()> {
    let mut buf: Vec<Memo> = Vec::with_capacity(cfg.batch_size);
    let mut last_tpl: Option<(String, i32, i64)> = None;
    let mut last_flush = Instant::now();
    let mut stream = consumer.stream();

    loop {
        let until_flush = cfg.flush_interval.saturating_sub(last_flush.elapsed());
        let flush_timeout = if until_flush.is_zero() {
            Duration::from_millis(1)
        } else {
            until_flush
        };

        tokio::select! {
            _ = shutdown_rx.changed() => {
                info!("memo-sink: shutdown received, draining");
                flush_and_commit(&consumer, &store, &mut buf, &last_tpl).await;
                return Ok(());
            }
            maybe = stream.next() => {
                match maybe {
                    Some(Ok(msg)) => {
                        let payload = match msg.payload() {
                            Some(p) => p,
                            None => {
                                warn!("memo-sink: empty payload, skipping");
                                continue;
                            }
                        };
                        match serde_json::from_slice::<MemoEnvelope>(payload) {
                            Ok(env) => {
                                buf.push(env.memo);
                                last_tpl = Some((msg.topic().to_string(), msg.partition(), msg.offset()));
                                if buf.len() >= cfg.batch_size {
                                    if flush_and_commit(&consumer, &store, &mut buf, &last_tpl).await {
                                        last_flush = Instant::now();
                                    }
                                }
                            }
                            Err(e) => {
                                warn!(error = %e, "memo-sink: envelope parse failed, skipping");
                            }
                        }
                    }
                    Some(Err(e)) => {
                        error!(error = %e, "memo-sink: consumer error");
                        sleep(Duration::from_millis(500)).await;
                    }
                    None => {
                        info!("memo-sink: stream ended");
                        flush_and_commit(&consumer, &store, &mut buf, &last_tpl).await;
                        return Ok(());
                    }
                }
            }
            _ = sleep(flush_timeout) => {
                if !buf.is_empty() {
                    if flush_and_commit(&consumer, &store, &mut buf, &last_tpl).await {
                        last_flush = Instant::now();
                    }
                } else {
                    last_flush = Instant::now();
                }
            }
        }
    }
}

async fn flush_and_commit(
    consumer: &StreamConsumer,
    store: &Arc<dyn EdgeStore>,
    buf: &mut Vec<Memo>,
    last: &Option<(String, i32, i64)>,
) -> bool {
    if buf.is_empty() {
        return true;
    }
    let count = buf.len();
    match store.insert_memos(buf).await {
        Ok(()) => {
            debug!(memos = count, "memo-sink: flushed batch");
            if let Some((topic, partition, offset)) = last {
                let mut tpl = TopicPartitionList::new();
                if let Err(e) =
                    tpl.add_partition_offset(topic, *partition, Offset::Offset(*offset + 1))
                {
                    warn!(error = %e, "memo-sink: tpl add failed");
                } else if let Err(e) = consumer.commit(&tpl, CommitMode::Async) {
                    warn!(error = %e, "memo-sink: commit failed");
                }
            }
            buf.clear();
            true
        }
        Err(e) => {
            error!(error = %e, memos = count, "memo-sink: insert failed, will retry next tick");
            false
        }
    }
}
