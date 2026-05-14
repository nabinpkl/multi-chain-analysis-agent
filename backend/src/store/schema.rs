use clickhouse::Client;

/// Bootstraps the storage schema. The edges table carries every
/// fungible value movement (SOL via `mint = ''`, SPL/Token-2022 via
/// `mint = <pubkey>`). Run on every startup; safe to call repeatedly.
///
/// The DROP on edges is intentional. The schema gained a `mint` column
/// when the parser switched from instruction-level (SOL only) to
/// balance-diff (any token). Old rows have no mint context and can't
/// be backfilled, so we wipe and rebuild from the ingestion checkpoint.
pub async fn bootstrap(client: &Client) -> anyhow::Result<()> {
    client
        .query("CREATE DATABASE IF NOT EXISTS multichain")
        .execute()
        .await?;

    client
        .query("DROP TABLE IF EXISTS multichain.edges")
        .execute()
        .await?;

    client
        .query(
            r#"
            CREATE TABLE multichain.edges (
                signature       String,
                instruction_idx UInt16,
                slot            UInt64,
                block_time      UInt32,
                from_wallet     String,
                to_wallet       String,
                amount          UInt64,
                mint            String,
                kind            LowCardinality(String),
                version         UInt64
            ) ENGINE = ReplacingMergeTree(version)
            ORDER BY (signature, instruction_idx)
            "#,
        )
        .execute()
        .await?;

    client
        .query(
            r#"
            CREATE TABLE IF NOT EXISTS multichain.ingestion_state (
                component   String,
                last_slot   UInt64,
                updated_at  DateTime
            ) ENGINE = ReplacingMergeTree(updated_at)
            ORDER BY (component)
            "#,
        )
        .execute()
        .await?;

    // Lazy-backfill cache for `/primitive/get_token_info`. First time a
    // mint is asked about, the handler hits RPC, decodes the metadata,
    // and writes a row here. Subsequent reads serve from the table
    // until `fetched_at_slot` falls outside the configured TTL window
    // (METADATA_CACHE_TTL_SLOTS, ~1 hour by default), at which point
    // the next read re-fetches and overwrites. ReplacingMergeTree by
    // `fetched_at_slot` so future CDC writes (issue #48) naturally win
    // over older lazy-backfill rows for the same mint.
    client
        .query(
            r#"
            CREATE TABLE IF NOT EXISTS multichain.token_metadata (
                mint             String,
                name             String,
                symbol           String,
                uri              String,
                update_authority String,
                source_program   LowCardinality(String),
                fetched_at_slot  UInt64,
                updated_at       DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree(fetched_at_slot)
            ORDER BY mint
            "#,
        )
        .execute()
        .await?;

    // Ship 1 of agent-observability (ADR 13) replaced the bespoke
    // multichain.agent_ledger table with OTel spans in otel.otel_traces
    // (auto-managed by the otel-collector clickhouseexporter). The
    // CREATE TABLE that used to live here was deleted along with the
    // agent_ledger writer module in agent-service.

    Ok(())
}
