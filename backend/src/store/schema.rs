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

    // Agent action ledger. Per phase 04 + ship-1 plan: append-only,
    // partitioned by day, ordered by (session_id, sequence) for cheap
    // session replay. TTL drops rows older than 90 days. Cost columns
    // exist now and are zero until ship 4 fills them.
    client
        .query(
            r#"
            CREATE TABLE IF NOT EXISTS multichain.agent_ledger (
                session_id            String,
                sequence              UInt64,
                timestamp_ms          UInt64,
                kind                  LowCardinality(String),
                principal_hash        String,
                payload               String,
                payload_hash          String,
                pre_estimate_units    UInt32,
                post_actual_units     UInt32,
                cost_relevant         UInt8,
                redaction_policy_ver  UInt32,
                inserted_at           DateTime DEFAULT now()
            ) ENGINE = MergeTree()
            PARTITION BY toYYYYMMDD(toDateTime(timestamp_ms / 1000))
            ORDER BY (session_id, sequence)
            TTL toDateTime(timestamp_ms / 1000) + INTERVAL 90 DAY
            SETTINGS index_granularity = 8192
            "#,
        )
        .execute()
        .await?;

    Ok(())
}
