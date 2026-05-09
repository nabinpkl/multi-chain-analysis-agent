//! ClickHouse-backed lazy cache for `OnChainMetadata`. Read path checks
//! the table; if there's a row whose `fetched_at_slot` is within the
//! configured TTL of the chain tip, return it. Otherwise the caller
//! re-fetches via RPC and writes through.
//!
//! TTL bounds staleness during the gap before issue #48 (CDC instruction
//! decoding) lands. After CDC, the TTL refresh path becomes dead code;
//! the materialized view is kept fresh by ingest-time writes and reads
//! never need to re-fetch.

use clickhouse::Client;
use clickhouse::Row;
use serde::{Deserialize, Serialize};
use tracing::debug;

use super::fetch::OnChainMetadata;

/// One row per mint in `multichain.token_metadata`. Field order matches
/// the schema in `store::schema::bootstrap`.
#[derive(Row, Serialize, Deserialize)]
struct MetadataRow {
    mint: String,
    name: String,
    symbol: String,
    uri: String,
    update_authority: String,
    source_program: String,
    fetched_at_slot: u64,
}

/// Read a cached row for `mint`. Returns `Some` when a row exists AND
/// `fetched_at_slot` is within `ttl_slots` of `current_slot`. Returns
/// `None` for "no row" and "row is stale" alike; the caller treats both
/// as "go fetch from RPC."
///
/// `current_slot` should come from `state.tip.current()`. If the tip is
/// not yet known (process just started, no `getSlot` round-trip has
/// landed), the caller should pass `0` so every cached row is treated
/// as stale and we fall through to live RPC.
pub async fn read_cached(
    client: &Client,
    mint_b58: &str,
    current_slot: u64,
    ttl_slots: u64,
) -> Result<Option<OnChainMetadata>, clickhouse::error::Error> {
    let row: Option<MetadataRow> = client
        .query(
            "SELECT mint, name, symbol, uri, update_authority, source_program, fetched_at_slot \
             FROM multichain.token_metadata FINAL \
             WHERE mint = ? \
             LIMIT 1",
        )
        .bind(mint_b58)
        .fetch_optional()
        .await?;

    let Some(row) = row else {
        debug!(mint = %mint_b58, "metadata cache: miss");
        return Ok(None);
    };

    // current_slot=0 sentinel = tip unknown, force re-fetch.
    if current_slot == 0 || current_slot.saturating_sub(row.fetched_at_slot) > ttl_slots {
        debug!(
            mint = %mint_b58,
            fetched_at_slot = row.fetched_at_slot,
            current_slot,
            ttl_slots,
            "metadata cache: stale, will re-fetch"
        );
        return Ok(None);
    }

    debug!(mint = %mint_b58, fetched_at_slot = row.fetched_at_slot, "metadata cache: hit");

    let program: &'static str = match row.source_program.as_str() {
        "metaplex" => "metaplex",
        "token2022" => "token2022",
        // Forward compat: a future writer may stamp a program label we
        // don't recognize here. Treat as cache miss so the caller
        // re-fetches via RPC; safer than fabricating a label.
        other => {
            debug!(
                mint = %mint_b58,
                source_program = %other,
                "metadata cache: unrecognized source_program label, treating as miss"
            );
            return Ok(None);
        }
    };

    Ok(Some(OnChainMetadata {
        name: row.name,
        symbol: row.symbol,
        uri: row.uri,
        update_authority: row.update_authority,
        program,
    }))
}

/// Write-through. Called by the fetch path after a successful RPC
/// resolution. Idempotent: ReplacingMergeTree collapses on `mint` with
/// `fetched_at_slot` as the version, so duplicate writes for the same
/// mint at the same slot are a no-op and a later write at a higher slot
/// wins.
///
/// `current_slot` should be the chain tip at the time of the RPC fetch
/// (from `state.tip.current()`). When tip is unknown, callers should
/// pass `0`; the row is still written (so subsequent reads have
/// SOMETHING to serve), but the next cache check will see
/// `current_slot.saturating_sub(0) > ttl` for any non-zero `current`,
/// which forces a refresh as soon as the tip is known.
pub async fn write_cached(
    client: &Client,
    mint_b58: &str,
    metadata: &OnChainMetadata,
    current_slot: u64,
) -> Result<(), clickhouse::error::Error> {
    let row = MetadataRow {
        mint: mint_b58.to_string(),
        name: metadata.name.clone(),
        symbol: metadata.symbol.clone(),
        uri: metadata.uri.clone(),
        update_authority: metadata.update_authority.clone(),
        source_program: metadata.program.to_string(),
        fetched_at_slot: current_slot,
    };
    let mut insert = client.insert("multichain.token_metadata")?;
    insert.write(&row).await?;
    insert.end().await?;
    debug!(
        mint = %mint_b58,
        fetched_at_slot = current_slot,
        "metadata cache: write-through"
    );
    Ok(())
}
