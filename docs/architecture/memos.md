# Memo capture: schema decision

This note documents why the SPL Memo capture path is shaped the way it is. Read this before changing the memo schema or the topic boundary.

## Sizing inputs (May 2026 mainnet)

Measured on live `getBlock` responses, jsonParsed encoding, transactionDetails=full, rewards=false:

| Slice | min | p50 | avg | p95 | max |
|---|---|---|---|---|---|
| memo program instructions | 117 B | 405 B | **447 B** | 1.1 KB | 1.1 KB |
| logMessages (all) | 682 KB | 879 KB | **831 KB** | 1.06 MB | 1.06 MB |
| whole block | 4.6 MB | 5.4 MB | 5.3 MB | 6.5 MB | 6.5 MB |
| memo instructions per block | 1 | 3 | 3.4 | 9 | 9 |

Two consequences:

- **Memo program instructions are essentially free.** ~447 B per block. At chain tip (2.5 slots/sec): ~100 MB/day uncompressed, ~15-20 MB/day compressed. Years of retention on a small disk.
- **logMessages are not affordable.** 831 KB per block × chain tip = ~30 GB/day compressed. Same wall as raw blocks. Filtered log capture (e.g. `Program log: <memo>` only) is brittle and adds nothing the parsed memo instruction doesn't already have. Don't capture logMessages.

## Why a sibling Kafka topic, not a wider Edge

Two real options were considered.

**Option A (chosen): sibling Kafka topic `solana.memos.v1`** with a `Memo` envelope, signature-keyed, joinable to `Edge` by signature.

**Option B (rejected): widen `Edge`** with `Option<Vec<Memo>>` per row.

Reasons A wins:

- Most signatures have zero memos. Option B pollutes the dense edge schema with a sparse field every consumer pays for.
- Different consumer profile. `Edge` is numeric: graph engine, analytics windows, ClickHouse aggregations. `Memo` is text: agent primitives, possibly future search. Forcing them into one row couples two disjoint reader sets.
- Option B forces a breaking change to `multichain.edges` ClickHouse table. Option A is purely additive.
- Same partition key (signature) keeps Edge+Memo joins cheap on the same partition. No cross-partition overhead vs the in-row option.

## Schema

Rust struct in `backend/src/domain.rs`:

```rust
#[derive(Debug, Clone, Row, Serialize, Deserialize)]
pub struct Memo {
    /// base58 tx signature. Joins to Edge.signature.
    pub signature: String,
    pub slot: u64,
    pub block_time: u32,
    /// Position within the tx. Top-level instructions and inner-
    /// instructions share one ascending namespace (top-level first,
    /// then each inner-instruction group in order). Pairs with
    /// `is_inner` to identify the source.
    pub instruction_idx: u16,
    pub is_inner: bool,
    /// Memo program version: "v1" (Memo1U…) or "v2" (MemoSq…).
    pub program: String,
    /// The actual memo text. UTF-8 string, may be empty.
    /// THIS is the only untrusted-text-bearing field in the whole
    /// pipeline today; the channel switch `external_text_input_enabled`
    /// gates whether the agent sees it raw or sanitized.
    pub memo_text: String,
    /// Signers required by the memo program (always at least one).
    pub signers: Vec<String>,
    /// ReplacingMergeTree version, set to ingest epoch_ms.
    pub version: u64,
}
```

Kafka envelope (parallel to `EnvelopeRef`/`Envelope` for Edge):

```json
{ "v": 1, "memo": { ...Memo... } }
```

Topic: `solana.memos.v1`. Auto-created by Redpanda on first publish (existing convention).

Partition key: `signature`.

ClickHouse table:

```sql
CREATE TABLE multichain.memos (
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
```

ORDER BY includes `is_inner` because top-level and inner-instruction memos can share an `instruction_idx` namespace in degenerate cases; the ReplacingMergeTree dedupe needs uniqueness.

## Why Rust struct + JSON, not protobuf

The repo's "no hand-typed wire types" rule is intended for cross-language wires. The Kafka producer and both consumers (ch-sink, possible future memo consumer) are Rust. Matches the existing `Edge` pattern, which is also a Rust struct + JSON envelope.

**Both `Edge` and `Memo` are technical debt against the wire-types rule and should migrate to proto in a separate ticket.** The migration becomes load-bearing the moment a non-Rust service needs to consume either topic. Until then, the migration cost (changes to ingester, ch-sink, graph engine consumer all at once) outweighs the consistency win.

## Coupling to the agent's input-text channel

`channels.externalTextInputEnabled` (the cockpit channel switch added with #36 phase 4) was vacuous before this work because no primitive emitted free text. With memos in the pipeline, an agent-side `get_wallet_memos` (or similar) primitive is the natural surface that opens the channel.

When that primitive lands, the sanitizer behind `externalTextInputEnabled=false` replaces `memo_text` with a placeholder before the data is wrapped in `<external_data>` for the agent. The boundary check (`wrap_external_data` in `agent-service/src/agent_service/boundary.py`) continues to fire in both states; the channel switch only affects whether the wrapped content carries real memo text or a redacted shape.

## Out of scope here

- Agent-side memo primitive: belongs with the article runner (#37).
- Sanitizer implementation: lands when the primitive does.
- Eval cases for memo handling (channel-on baseline, indirect-injection probe): same dependency as the primitive.
- Migrating Edge + Memo to proto: separate ticket.
- Capturing logMessages: rejected on cost (see Sizing).
