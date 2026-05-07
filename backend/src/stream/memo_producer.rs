use std::time::Duration;

use rdkafka::ClientConfig;
use rdkafka::producer::{FutureProducer, FutureRecord, Producer};
use tracing::warn;

use crate::domain::Memo;
use crate::stream::topics::MemoEnvelopeRef;

/// Memo Kafka publisher. Mirror of `EdgeProducer` against the
/// `solana.memos.v1` topic; same producer settings (idempotence,
/// acks=all, lz4, linger=20ms, retries=10) so retried slots dedupe at
/// the broker. Partition key is the tx signature so same-tx memos
/// land on one partition and stay co-partitioned with the corresponding
/// edges in `solana.raw-edges`.
#[derive(Clone)]
pub struct MemoProducer {
    inner: FutureProducer,
    topic: String,
}

impl MemoProducer {
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
        })
    }

    pub async fn publish(&self, memo: &Memo) -> anyhow::Result<()> {
        let payload = serde_json::to_vec(&MemoEnvelopeRef::wrap(memo))?;
        let key = memo.signature.as_str();

        let record = FutureRecord::to(&self.topic).key(key).payload(&payload);

        match self.inner.send(record, Duration::from_secs(10)).await {
            Ok(_) => Ok(()),
            Err((e, _)) => {
                warn!(error = %e, "kafka memo produce failed");
                Err(anyhow::anyhow!(e))
            }
        }
    }

    pub async fn flush(&self, timeout: Duration) {
        let _ = self.inner.flush(timeout);
    }
}
