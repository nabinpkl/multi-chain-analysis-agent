//! Generic Kafka publisher parameterized over an `IngestStream`. One
//! rdkafka `FutureProducer` per stream type; same producer settings
//! across all streams (idempotence, acks=all, lz4, linger=20ms,
//! retries=10) so retried slots dedupe at the broker.
//!
//! Adding a new stream type is now `StreamProducer::<MyStream>::new(
//! brokers, topic)`; no new producer module per row type.

use std::marker::PhantomData;
use std::time::Duration;

use rdkafka::ClientConfig;
use rdkafka::producer::{FutureProducer, FutureRecord, Producer};
use tracing::warn;

use crate::stream::ingest_stream::IngestStream;

pub struct StreamProducer<S: IngestStream> {
    inner: FutureProducer,
    topic: String,
    _marker: PhantomData<fn() -> S>,
}

impl<S: IngestStream> Clone for StreamProducer<S> {
    fn clone(&self) -> Self {
        Self {
            inner: self.inner.clone(),
            topic: self.topic.clone(),
            _marker: PhantomData,
        }
    }
}

impl<S: IngestStream> StreamProducer<S> {
    pub fn new(brokers: &str, topic: impl Into<String>) -> anyhow::Result<Self> {
        let inner: FutureProducer = ClientConfig::new()
            .set("bootstrap.servers", brokers)
            .set("enable.idempotence", "true")
            .set("acks", "all")
            .set("compression.type", "lz4")
            .set("linger.ms", "20")
            .set("retries", "10")
            .set("message.timeout.ms", "30000")
            .create()?;

        Ok(Self {
            inner,
            topic: topic.into(),
            _marker: PhantomData,
        })
    }

    pub async fn publish(&self, row: &S::Row) -> anyhow::Result<()> {
        let payload = S::wrap_envelope(row);
        let key = S::partition_key(row);
        let record = FutureRecord::to(&self.topic).key(key).payload(&payload);

        match self.inner.send(record, Duration::from_secs(10)).await {
            Ok(_) => Ok(()),
            Err((e, _)) => {
                warn!(stream = S::NAME, error = %e, "kafka publish failed");
                Err(anyhow::anyhow!(e))
            }
        }
    }

    pub async fn flush(&self, timeout: Duration) {
        let _ = self.inner.flush(timeout);
    }
}
