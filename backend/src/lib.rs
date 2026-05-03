//! Library face of the multichain backend. Exposes the same modules
//! `main.rs` declares so other binaries in this crate (notably
//! `dump_schemas`) can reach them.
//!
//! Phase A of the Python-agent migration introduced this lib; before
//! that everything was bin-only. The `[[bin]]` targets share `main.rs`
//! conventions but cannot reach each other's modules without going
//! through a library crate.

pub mod agent;
pub mod analytics;
pub mod api;
pub mod config;
pub mod domain;
pub mod graph;
pub mod ingest;
pub mod rpc;
pub mod sinks;
pub mod state;
pub mod store;
pub mod stream;
pub mod tip;
pub mod wire;
