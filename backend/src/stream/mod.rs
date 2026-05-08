pub mod consumer;
pub mod ingest_stream;
pub mod stream_producer;
pub mod topics;

pub use ingest_stream::{EdgeStream, IngestStream, MetadataStream};
pub use stream_producer::StreamProducer;

/// Type alias for the existing edge call sites. New stream types use
/// `StreamProducer<MyStream>` directly; this alias keeps the
/// pre-abstraction `EdgeProducer` name in places where churn is not
/// worth it (runner signature, `main.rs` wiring).
pub type EdgeProducer = StreamProducer<EdgeStream>;

/// Sibling alias for token metadata. Keeps runner / main signatures
/// readable at call sites.
pub type MetadataProducer = StreamProducer<MetadataStream>;
