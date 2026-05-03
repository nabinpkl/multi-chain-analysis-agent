//! Wire types crossing service boundaries.
//!
//! Single source of truth: `proto/multichain/wire/{shared,agent}/v1/*.proto`.
//! `just regen-wire-types` runs `buf generate` against those, producing the
//! Rust types in `generated/`. Cross-language consumers regenerate from
//! the same protos to their own languages.
//!
//! `proto_bridge` converts between the generated proto types and the
//! internal Rust shapes the compute primitives produce
//! (`crate::primitives::types::*`). See the module header for why the
//! indirection exists.

#[path = "generated/mod.rs"]
pub mod generated;

pub mod proto_bridge;
