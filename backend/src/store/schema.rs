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

    // Memo table. One row per SPL Memo program instruction (top-level
    // or CPI'd inner). Joins to `multichain.edges` by signature for
    // analytics that pair value movement with attached note text.
    // See `docs/architecture/memos.md` for the schema rationale.
    client
        .query(
            r#"
            CREATE TABLE IF NOT EXISTS multichain.memos (
                signature       String,
                slot            UInt64,
                block_time      UInt32,
                instruction_idx UInt16,
                is_inner        Bool,
                program         LowCardinality(String),
                memo_text       String,
                signers         Array(String),
                version         UInt64
            ) ENGINE = ReplacingMergeTree(version)
            ORDER BY (signature, instruction_idx, is_inner)
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
