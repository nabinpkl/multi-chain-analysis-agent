//! Generic abstraction for "things we capture from `getBlock` and
//! ship to ClickHouse via Kafka." Each implementor pins:
//!
//! - the row type to ship,
//! - the partition-key extractor (so ordering and co-partitioning are
//!   each impl's choice),
//! - the wire-envelope wrap/unwrap (so the `{ v, body }` shape stays
//!   uniform across topics),
//! - the insert dispatch onto `EdgeStore` (each impl picks the right
//!   per-row-type method; keeps `EdgeStore` object-safe).
//!
//! `IngestStream` is used statically (`StreamProducer<S>`,
//! `stream_sink::run::<S>`); never as a trait object. That lets the
//! per-row insert path stay monomorphized while `EdgeStore` (which is
//! held behind `Arc<dyn ...>`) keeps to non-generic methods and stays
//! dyn-compatible.
//!
//! Adding a new stream type is now ~one impl plus a topic env var
//! plus a per-row store method. See `EdgeStream` below as the
//! reference; future impls follow the same shape.

use std::future::Future;
use std::sync::Arc;

use clickhouse::Row;
use serde::Serialize;
use serde::de::DeserializeOwned;

use crate::domain::{Edge, TokenMetadataEvent};
use crate::store::EdgeStore;
use crate::stream::topics::{
    Envelope, EnvelopeRef, TokenMetadataEnvelope, TokenMetadataEnvelopeRef,
};

pub trait IngestStream: Send + Sync + 'static {
    /// The deserialized Rust row type.
    type Row: Serialize + DeserializeOwned + Row + Send + Sync + Clone + 'static;

    /// Display name for log spans (e.g. "edge", "token-metadata").
    /// Threaded through producer + sink logs as `stream = NAME`.
    const NAME: &'static str;

    /// Partition key extractor. Edges use the tx signature so same-tx
    /// edges stay co-partitioned. Future streams keyed on `mint` (e.g.
    /// token metadata) keep "everything for mint M" on one partition.
    fn partition_key(row: &Self::Row) -> &str;

    /// Serialize the row inside a `{ v, <body> }` envelope. The
    /// borrowed-envelope avoids cloning the row at publish time.
    fn wrap_envelope(row: &Self::Row) -> Vec<u8>;

    /// Deserialize an envelope back to the row type.
    fn unwrap_envelope(payload: &[u8]) -> anyhow::Result<Self::Row>;

    /// Dispatch a batch insert through the store. Each impl picks the
    /// appropriate per-row-type store method, which keeps `EdgeStore`
    /// object-safe (no generic methods on the trait).
    fn insert<'a>(
        store: &'a Arc<dyn EdgeStore>,
        rows: &'a [Self::Row],
    ) -> impl Future<Output = anyhow::Result<()>> + Send + 'a;
}

/// Edge stream. Every wallet-to-wallet fungible movement parsed from
/// pre/post balance diffs by `ingest::parser::parse_edges`. Topic
/// `solana.raw-edges`, table `multichain.edges`, partition key tx
/// signature.
pub struct EdgeStream;

impl IngestStream for EdgeStream {
    type Row = Edge;
    const NAME: &'static str = "edge";

    fn partition_key(row: &Edge) -> &str {
        &row.signature
    }

    fn wrap_envelope(row: &Edge) -> Vec<u8> {
        serde_json::to_vec(&EnvelopeRef::wrap(row)).expect("edge envelope serialize")
    }

    fn unwrap_envelope(payload: &[u8]) -> anyhow::Result<Edge> {
        let env: Envelope = serde_json::from_slice(payload)?;
        Ok(env.edge)
    }

    fn insert<'a>(
        store: &'a Arc<dyn EdgeStore>,
        rows: &'a [Edge],
    ) -> impl Future<Output = anyhow::Result<()>> + Send + 'a {
        async move { store.insert_edges(rows).await }
    }
}

/// Token metadata stream. Metaplex Token Metadata Program writes
/// (CreateMetadataAccountV2 / V3 today) decoded by
/// `ingest::metadata::parse_token_metadata`. Future Token-2022 stream-
/// decode work emits into the same row type with `program="token2022"`.
/// Topic `solana.token_metadata.v1`, table `multichain.token_metadata`.
/// Partition key is the mint pubkey so a future "everything for mint M"
/// consumer finds all events on one partition.
pub struct MetadataStream;

impl IngestStream for MetadataStream {
    type Row = TokenMetadataEvent;
    const NAME: &'static str = "token-metadata";

    fn partition_key(row: &TokenMetadataEvent) -> &str {
        &row.mint
    }

    fn wrap_envelope(row: &TokenMetadataEvent) -> Vec<u8> {
        serde_json::to_vec(&TokenMetadataEnvelopeRef::wrap(row))
            .expect("token metadata envelope serialize")
    }

    fn unwrap_envelope(payload: &[u8]) -> anyhow::Result<TokenMetadataEvent> {
        let env: TokenMetadataEnvelope = serde_json::from_slice(payload)?;
        Ok(env.token_metadata)
    }

    fn insert<'a>(
        store: &'a Arc<dyn EdgeStore>,
        rows: &'a [TokenMetadataEvent],
    ) -> impl Future<Output = anyhow::Result<()>> + Send + 'a {
        async move { store.insert_token_metadata(rows).await }
    }
}
