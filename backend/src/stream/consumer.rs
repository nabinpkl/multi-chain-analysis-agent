use rdkafka::ClientConfig;
use rdkafka::consumer::{Consumer, StreamConsumer};

pub fn build_consumer(
    brokers: &str,
    group_id: &str,
    topic: &str,
    auto_offset_reset: &str,
) -> anyhow::Result<StreamConsumer> {
    let consumer: StreamConsumer = ClientConfig::new()
        .set("bootstrap.servers", brokers)
        .set("group.id", group_id)
        .set("enable.auto.commit", "false")
        .set("auto.offset.reset", auto_offset_reset)
        .set("session.timeout.ms", "10000")
        .set("max.poll.interval.ms", "300000")
        .create()?;

    consumer.subscribe(&[topic])?;
    Ok(consumer)
}
