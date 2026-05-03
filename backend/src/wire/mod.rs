//! Wire types crossing service boundaries.
//!
//! Single source of truth: `proto/multichain/wire/{shared,agent}/v1/*.proto`.
//! `just regen-wire-types` runs `buf generate` against those, producing the
//! Rust types in `generated/`. Cross-language consumers regenerate from
//! the same protos to their own languages.
//!
//! `proto_bridge` is a transitional adapter between the proto types and
//! the still-alive internal Rust types in `crate::agent::*`. Phase C
//! deletes the bridge with the agent module.

#[path = "generated/mod.rs"]
pub mod generated;

// TRANSITIONAL: bridges proto-generated types  internal Rust types
// in `crate::agent::*`. Removed in Phase C alongside the agent module.
// See file header for rationale.
#[path = "_proto_bridge.rs"]
pub mod proto_bridge;
