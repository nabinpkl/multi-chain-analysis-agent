use std::time::Duration;

use rdkafka::ClientConfig;
use rdkafka::producer::{FutureProducer, FutureRecord, Producer};
use tracing::warn;

use crate::domain::Edge;
use crate::stream::topics::EnvelopeRef;

#[derive(Clone)]
pub struct EdgeProducer {
    inner: FutureProducer,
    topic: String,
}

impl EdgeProducer {
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

    pub async fn publish(&self, edge: &Edge) -> anyhow::Result<()> {
        let payload = serde_json::to_vec(&EnvelopeRef::wrap(edge))?;
        let key = edge.signature.as_str();

        let record = FutureRecord::to(&self.topic)
            .key(key)
            .payload(&payload);

        match self
            .inner
            .send(record, Duration::from_secs(10))
            .await
        {
            Ok(_) => Ok(()),
            Err((e, _)) => {
                warn!(error = %e, "kafka produce failed");
                Err(anyhow::anyhow!(e))
            }
        }
    }

    pub async fn flush(&self, timeout: Duration) {
        let _ = self.inner.flush(timeout);
    }
}
