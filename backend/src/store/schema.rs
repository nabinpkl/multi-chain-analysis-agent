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

    // The memo capture pipeline was removed when audit found zero
    // downstream consumers. Drop any leftover table from prior runs.
    // No corresponding CREATE; the table is intentionally gone.
    client
        .query("DROP TABLE IF EXISTS multichain.memos")
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

    // Token metadata table. One row per Metaplex Token Metadata
    // Program write (CreateMetadataAccountV2 / V3 today; Token-2022
    // metadata-extension writes when that decoder lands). The
    // `program` column distinguishes source so multiple programs share
    // the table cleanly. Current metadata per mint is a query
    // (`ORDER BY slot DESC LIMIT 1`), not a materialized view. See
    // `docs/architecture/token-metadata-ingestion.md`.
    client
        .query(
            r#"
            CREATE TABLE IF NOT EXISTS multichain.token_metadata (
                mint              String,
                metadata_pda      String,
                signature         String,
                slot              UInt64,
                block_time        UInt32,
                instruction_idx   UInt16,
                is_inner          Bool,
                program           LowCardinality(String),
                op                LowCardinality(String),
                name              String,
                symbol            String,
                uri               String,
                update_authority  String,
                version           UInt64
            ) ENGINE = ReplacingMergeTree(version)
            ORDER BY (mint, signature, instruction_idx, is_inner)
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
