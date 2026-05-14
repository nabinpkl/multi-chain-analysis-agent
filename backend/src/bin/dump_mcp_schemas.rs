//! Dumps the `McaeMcp` MCP `tools/list` schema snapshot.
//!
//! The hermetic-eval mock substrate (at
//! `evals/cases-hermetic/mock-service/`) advertises tool schemas to
//! codex by loading this snapshot at startup, so the mock never
//! hand-types JSON Schema definitions in Python. Backend MCP source
//! at `backend/src/mcp.rs` stays the single source of truth; this
//! binary is the build-time bridge.
//!
//! Usage:
//!   cargo run --bin dump-mcp-schemas > evals/cases-hermetic/mock-service/schemas.json
//!   # or via the justfile:
//!   just dump-mcp-schemas
//!
//! A drift test in `backend/src/mcp.rs::tests::schemas_snapshot_matches`
//! loads the checked-in snapshot and compares against the live
//! `ToolRouter::list_all()` output. If they differ, the test fails and
//! prompts a snapshot regen.

use multichain_engine::mcp::McaeMcp;

fn main() {
    let tools = McaeMcp::schemas();
    let json =
        serde_json::to_string_pretty(&tools).expect("rmcp Tool descriptors are serde::Serialize");
    println!("{json}");
}
