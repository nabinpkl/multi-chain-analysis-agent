//! Wire types crossing service boundaries. Phase A of the Python-agent
//! migration introduced this module as the source-of-truth for shapes
//! shared between Rust (data plane on :8002) and Python (agent plane
//! on :8003), plus the existing Rust→Frontend ts-rs flow.
//!
//! The `shared` submodule holds re-exports of types that live in their
//! pre-migration locations (so existing Rust callers keep working
//! during the migration), plus brand-new types that didn't exist
//! before (the snapshot lease envelope shapes).
//!
//! `cargo run --bin dump_schemas` walks every type listed here and
//! writes one JSON Schema file per type to
//! `agent-service/src/agent_service/wire/schemas-shared/`. The Python
//! side then runs `datamodel-code-generator` on those schemas to
//! produce `agent-service/src/agent_service/wire/shared.py`, the
//! source of truth for pydantic models on the Python side.
//!
//! Wired together by the `regen-wire-types` recipe in the root
//! `justfile`. Pre-commit hook enforces no drift.

pub mod shared;

// Generated proto types (smoke-test placement; the migration will
// remove `shared` once consumers move over).
#[path = "generated/mod.rs"]
pub mod generated;
