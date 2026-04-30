//! Action ledger: append-only structured record of every prompt, tool
//! call, claim emission, etc. The substrate that ship 4 (drift), ship
//! 6 (eval replay), and ship 7 (pulse) all read.
//!
//! v0 deploys the schema, owns per-session sequence counters, and
//! writes synchronously. Phase 04 promotes to async batched + content
//! hashing rigor + retention TTL.

pub mod event;
pub mod replay;
pub mod write;

pub use event::{LedgerEvent, LedgerEventKind};
pub use replay::replay_session;
pub use write::{Ledger, LedgerEventDraft};
