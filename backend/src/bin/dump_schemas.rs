//! Dump JSON Schema for every shared wire type.
//!
//! Walks the curated inventory in `wire::shared`, calls
//! `schemars::schema_for!(...)` on each, and writes one JSON file per
//! type to `agent-service/src/agent_service/wire/schemas-shared/`.
//! `datamodel-code-generator` then converts these into the pydantic
//! source-of-truth on the Python side.
//!
//! Run via `just regen-wire-types` (or directly: `cargo run --bin
//! dump_schemas`). Idempotent. The output directory is recreated each
//! run so removed types don't leave stale schema files behind.
//!
//! Adding a type: extend the `dump_one!` invocations in `main()`.
//! That's it; no other registration step.

use std::fs;
use std::path::PathBuf;

use anyhow::{Context, Result};
use schemars::{JsonSchema, schema_for};

// Pull `wire::shared` into scope. The binary lives in
// `backend/src/bin/`, so we need to also pull in the modules
// `wire::shared` re-exports (the `agent::*` and `analytics::*` paths)
// because `schema_for!` needs the type definitions visible.
//
// Trick: include the entire backend crate as a path-mod via
// `include!`-free composition; instead we just declare them via
// `use multichain_engine::wire::shared::*;` IF the crate is a library
// crate. Today multichain-engine is a binary-only crate. Two options:
// (a) move shared types into a tiny library crate; (b) declare the
// dump as a Cargo `[[bin]]` of the same crate.
//
// We go with (b): this file is a bin target in the same crate, so it
// can reach any pub item via `crate::...`.

use multichain_engine::wire::shared::*;

fn dump_schema<T: JsonSchema>(out_dir: &PathBuf, name: &str) -> Result<()> {
    let schema = schema_for!(T);
    let json = serde_json::to_string_pretty(&schema)
        .with_context(|| format!("serialize schema for {name}"))?;
    let path = out_dir.join(format!("{name}.json"));
    fs::write(&path, json + "\n").with_context(|| format!("write {}", path.display()))?;
    println!("wrote {}", path.display());
    Ok(())
}

fn main() -> Result<()> {
    let out_dir = std::env::var("DUMP_SCHEMAS_OUT_DIR").unwrap_or_else(|_| {
        // Default: assume cargo invocation from `backend/`. The agent-
        // service lives at the repo root, so go up one level.
        "../agent-service/src/agent_service/wire/schemas-shared".to_string()
    });
    let out_dir = PathBuf::from(out_dir);

    if out_dir.exists() {
        fs::remove_dir_all(&out_dir)
            .with_context(|| format!("clear {}", out_dir.display()))?;
    }
    fs::create_dir_all(&out_dir)
        .with_context(|| format!("create {}", out_dir.display()))?;

    println!("dumping schemas to {}", out_dir.display());

    // Inventory. Keep alphabetically sorted within each category.
    //
    // Re-exports of pre-existing types:
    dump_schema::<ClaimKind>(&out_dir, "ClaimKind")?;
    dump_schema::<CommunitySummaryInput>(&out_dir, "CommunitySummaryInput")?;
    dump_schema::<CommunitySummaryOutput>(&out_dir, "CommunitySummaryOutput")?;
    dump_schema::<EmitClaimInput>(&out_dir, "EmitClaimInput")?;
    dump_schema::<EmitClaimOutput>(&out_dir, "EmitClaimOutput")?;
    dump_schema::<NodeRole>(&out_dir, "NodeRole")?;
    dump_schema::<NodeStatsWire>(&out_dir, "NodeStatsWire")?;
    dump_schema::<NumberRef>(&out_dir, "NumberRef")?;
    dump_schema::<ProvenanceRef>(&out_dir, "ProvenanceRef")?;
    dump_schema::<SubgraphSlice>(&out_dir, "SubgraphSlice")?;
    dump_schema::<TimeScope>(&out_dir, "TimeScope")?;
    dump_schema::<TopCounterparty>(&out_dir, "TopCounterparty")?;
    dump_schema::<TopWallet>(&out_dir, "TopWallet")?;
    dump_schema::<WalletProfileInput>(&out_dir, "WalletProfileInput")?;
    dump_schema::<WalletProfileOutput>(&out_dir, "WalletProfileOutput")?;

    // New types introduced for Phase A:
    dump_schema::<CommunitySummaryRequest>(&out_dir, "CommunitySummaryRequest")?;
    dump_schema::<PrimitiveResponseEnvelope>(&out_dir, "PrimitiveResponseEnvelope")?;
    dump_schema::<SnapshotBeginResponse>(&out_dir, "SnapshotBeginResponse")?;
    dump_schema::<SnapshotEndRequest>(&out_dir, "SnapshotEndRequest")?;
    dump_schema::<WalletProfileRequest>(&out_dir, "WalletProfileRequest")?;

    println!("done");
    Ok(())
}
