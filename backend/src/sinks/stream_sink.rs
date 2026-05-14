//! Generic Kafka-to-ClickHouse sink parameterized over an
//! `IngestStream`. Reads envelopes off the consumer's topic, batches
//! by `batch_size`, flushes on size OR `flush_interval` (whichever
//! first), commits offsets after a successful insert.
//!
//! One sink task per stream type, each spawned in `main.rs`. Isolating
//! per-stream means a failure on one topic does not stall another
//! (e.g. a ClickHouse hiccup on the metadata topic does not block
//! edge ingestion).

use std::sync::Arc;
use std::time::Duration;

use futures_util::StreamExt;
use rdkafka::consumer::{CommitMode, Consumer, StreamConsumer};
use rdkafka::{Message, Offset, TopicPartitionList};
use tokio::sync::watch;
use tokio::time::{Instant, sleep};
use tracing::{debug, error, info, warn};

use crate::store::EdgeStore;
use crate::stream::ingest_stream::IngestStream;

/// Reused across stream types because all current streams have
/// similar batch and flush characteristics; if a future stream needs
/// different cadence, pass a different `SinkConfig` to its
/// `stream_sink::run` task.
pub struct SinkConfig {
    pub batch_size: usize,
    pub flush_interval: Duration,
}

pub async fn run<S: IngestStream>(
    consumer: StreamConsumer,
    store: Arc<dyn EdgeStore>,
    cfg: SinkConfig,
    mut shutdown_rx: watch::Receiver<bool>,
) -> anyhow::Result<()> {
    let mut buf: Vec<S::Row> = Vec::with_capacity(cfg.batch_size);
    let mut last_tpl: Option<(String, i32, i64)> = None;
    let mut last_flush = Instant::now();
    let mut stream = consumer.stream();
    let stream_name = S::NAME;

    loop {
        let until_flush = cfg.flush_interval.saturating_sub(last_flush.elapsed());
        let flush_timeout = if until_flush.is_zero() {
            Duration::from_millis(1)
        } else {
            until_flush
        };

        tokio::select! {
            _ = shutdown_rx.changed() => {
                info!(stream = stream_name, "stream-sink: shutdown received, draining");
                flush_and_commit::<S>(&consumer, &store, &mut buf, &last_tpl, stream_name).await;
                return Ok(());
            }
            maybe = stream.next() => {
                match maybe {
                    Some(Ok(msg)) => {
                        let payload = match msg.payload() {
                            Some(p) => p,
                            None => {
                                warn!(stream = stream_name, "stream-sink: empty payload, skipping");
                                continue;
                            }
                        };
                        match S::unwrap_envelope(payload) {
                            Ok(row) => {
                                buf.push(row);
                                last_tpl = Some((msg.topic().to_string(), msg.partition(), msg.offset()));
                                if buf.len() >= cfg.batch_size {
                                    if flush_and_commit::<S>(&consumer, &store, &mut buf, &last_tpl, stream_name).await {
                                        last_flush = Instant::now();
                                    }
                                }
                            }
                            Err(e) => {
                                warn!(stream = stream_name, error = %e, "stream-sink: envelope parse failed, skipping");
                            }
                        }
                    }
                    Some(Err(e)) => {
                        error!(stream = stream_name, error = %e, "stream-sink: consumer error");
                        sleep(Duration::from_millis(500)).await;
                    }
                    None => {
                        info!(stream = stream_name, "stream-sink: stream ended");
                        flush_and_commit::<S>(&consumer, &store, &mut buf, &last_tpl, stream_name).await;
                        return Ok(());
                    }
                }
            }
            _ = sleep(flush_timeout) => {
                if !buf.is_empty() {
                    if flush_and_commit::<S>(&consumer, &store, &mut buf, &last_tpl, stream_name).await {
                        last_flush = Instant::now();
                    }
                } else {
                    last_flush = Instant::now();
                }
            }
        }
    }
}

async fn flush_and_commit<S: IngestStream>(
    consumer: &StreamConsumer,
    store: &Arc<dyn EdgeStore>,
    buf: &mut Vec<S::Row>,
    last: &Option<(String, i32, i64)>,
    stream_name: &str,
) -> bool {
    if buf.is_empty() {
        return true;
    }
    let count = buf.len();
    match S::insert(store, buf).await {
        Ok(()) => {
            debug!(stream = stream_name, count, "stream-sink: flushed batch");
            if let Some((topic, partition, offset)) = last {
                let mut tpl = TopicPartitionList::new();
                if let Err(e) =
                    tpl.add_partition_offset(topic, *partition, Offset::Offset(*offset + 1))
                {
                    warn!(stream = stream_name, error = %e, "stream-sink: tpl add failed");
                } else if let Err(e) = consumer.commit(&tpl, CommitMode::Async) {
                    warn!(stream = stream_name, error = %e, "stream-sink: commit failed");
                }
            }
            buf.clear();
            true
        }
        Err(e) => {
            error!(stream = stream_name, error = %e, count, "stream-sink: insert failed, will retry next tick");
            false
        }
    }
}
