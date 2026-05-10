//! Library face of the multichain backend (data plane). Exposes the
//! modules the server binary declares so adjacent test binaries can
//! reach them.
//!
//! Phase C deleted the `agent` module entirely. The Python agent
//! service on `:8003` owns the agent plane end-to-end; this crate
//! is the data plane.

pub mod analytics;
pub mod api;
pub mod config;
pub mod domain;
pub mod graph;
pub mod ingest;
pub mod mcp;
pub mod metadata;
pub mod primitives;
pub mod rpc;
pub mod sinks;
pub mod snapshot;
pub mod state;
pub mod store;
pub mod stream;
pub mod tip;
pub mod util;
pub mod wire;
