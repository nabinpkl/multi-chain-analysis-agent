# Token metadata ingestion (on-chain leg)

Status (2026-05-08): **stream-decode was removed; lazy fetch is the only
path.** `multichain.token_metadata` is populated entirely by
`backend/src/metadata/fetch.rs`, which runs on demand when the agent
asks `get_token_info` for a mint not yet cached. Three confirmations
drove the rip-out:

- Most active mints (USDC, BONK, JUP) were created before any plausible
  ingest window, so a stream decoder never sees them. The agent only
  cares about mints it asks about; lazy-on-demand exactly matches that
  shape.
- Empirical mainnet sampling showed `CreateMetadataAccountV2/V3`
  (discriminators 16 / 33) is rare in 2026; most current "create"
  activity has shifted to pump.fun / Token-2022 / Metaplex Core.
- For Token-2022 in particular, `getAccountInfo` jsonParsed already
  structures the metadata extension; a stream decoder for that program
  added zero value over the lazy-fetch path.

What remains in the codebase: the row schema (`TokenMetadataEvent`),
the ClickHouse table, the lazy-fetch module, the `/primitive/get_token_info`
endpoint, and the agent tool. What was removed: `ingest::metadata`
(Metaplex borsh decoder), `MetadataStream` / `MetadataProducer`,
`token-metadata-sink` Kafka consumer, `KAFKA_TOPIC_TOKEN_METADATA`.

Everything below is preserved as historical context for the design
investigation; treat references to "stream decode in `parser.rs`",
`parse_token_metadata`, the Metaplex producer, etc. as describing a
state of the world that no longer exists.

---

## What we're capturing

Three fields per mint, sourced from on-chain accounts:

- `name` (Metaplex: max 32 bytes; Token-2022: variable, typically same range)
- `symbol` (Metaplex: max 10 bytes; Token-2022: variable)
- `uri` (Metaplex: max 200 bytes; Token-2022: variable)

Plus housekeeping: `mint`, `update_authority`, slot, signature, instruction position, `program` (`metaplex` | `token2022`), `op` (`create` | `update`), and a `version` for ReplacingMergeTree.

Per-row payload ~280 bytes uncompressed, ~150 bytes after LZ4. Storage is trivial at any plausible mint cardinality.

The off-chain JSON at the URI (description, image, header, social links) is a separate ingestion leg with a real HTTP fetch policy. Out of scope here.

## Encoding decision

Solana RPC's `jsonParsed` encoding decodes a hard-coded set of native-feeling programs: System, Stake, Vote, ComputeBudget, Address Lookup Table, SPL Token (legacy), SPL Token-2022 (with extensions), SPL Memo, BPF Loader, and a handful of others. The two programs we care about behave differently:

### Metaplex Token Metadata Program (`metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s`)

Outside the jsonParsed allowlist for both instructions and accounts.

- **Instruction in `getBlock`:** confirmed slot 418342223, signature `8YKtPWJvEm4Ybc7XnUZYvfibakP24fsZnaWeRsmnfiw6DjogATcNnwbUGzQoQtS7ChVooNmmEjgkYuh6PU8dXkY`. jsonParsed returns the instruction with `data` as a base58 string and no `parsed` field. Example data `D9kCuD4PTuQuyCK` decoded to 11 bytes `[0x31, 0x00, 0x01, ...]`, discriminator 49 (a v1-namespace update variant).
- **Account state via `getAccountInfo` jsonParsed:** confirmed against USDC's metadata PDA `5x38Kp4hvdomTCnCrAny4UtMUt5rQBdB6px2K1Ui45Wq`. Owner = Metaplex program; `data` falls through to base64 with 679 raw bytes. Manual borsh walk confirmed: `name="USD Coin"`, `symbol="USDC"`, `uri=""`. Layout: byte 0 = `Key` enum (4 = `MetadataV1`), bytes 1..33 = update_authority, 33..65 = mint, 65..69 = name_len, 69..101 = name (32 bytes fixed alloc), then symbol_len + symbol (10 bytes fixed), then uri_len + uri (200 bytes fixed).

**Metaplex decision: borsh-decode `inst.data` from `getBlock` in the
stream parser.** The 679-byte account-state layout is documented
above for completeness, but the backfill path that reads
`getAccountInfo` is intentionally not built (see "Historical-mint
coverage gap" below).

### SPL Token-2022 metadata extension (`TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb`)

Account state via `getAccountInfo` jsonParsed comes back already
structured (confirmed against PYUSD
`2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo`: the mint exposes a
`tokenMetadata` extension whose `state` carries `name`, `symbol`,
`uri`, `mint`, `updateAuthority`, `additionalMetadata` directly).
That makes any future read-side path easy. But it does not change
the stream-decode question.

Instructions in `getBlock` come back as raw base58 `data` for
Token-2022 metadata extension writes (`TokenMetadataInitialize`,
`TokenMetadataUpdateField`, etc.), same as Metaplex. They flow
through every block we already pull; capturing them costs zero
extra RPC.

**Token-2022 decision: stream-decode in `parser.rs`, mirroring the
Metaplex path.** Filter by program ID, dispatch on the extension's
sub-instruction discriminator, borsh-decode the args, emit a
`TokenMetadataEvent` with `program="token2022"`. The implementation
shape mirrors `parse_token_metadata` for Metaplex: same row type,
same parser entry point, same publish path. The bytes are arriving
either way; ignoring them would be the same "silently dropping
something we already receive" anti-pattern that motivated this
whole pipeline.

| Program | Stream-decode source |
|---|---|
| Metaplex Token Metadata | borsh on `inst.data` from `getBlock` |
| SPL Token-2022 metadata extension | borsh on `inst.data` from `getBlock` (extension-namespace dispatch) |

Both write to the same `multichain.token_metadata` table,
distinguished by the `program` column.

## What this implies for the existing pipeline

`backend/src/rpc/types.rs::RawInstruction` already carries `program_id`, `accounts`, and `parsed: Option<Value>`. To support borsh decoding it needs a `data: Option<String>` field too (jsonParsed populates `data` for instructions it doesn't natively decode). One-line addition.

## Discriminators to handle (initial set)

**Metaplex Token Metadata** (mpl-token-metadata legacy enum + v1 namespace):

- 16 = `CreateMetadataAccountV2` (deprecated but still present in the wild)
- 33 = `CreateMetadataAccountV3` (canonical create today)
- 15 = `UpdateMetadataAccountV2` (legacy update)
- 47 = `UpdateV1` (unified update under the v1 namespace)
- 49 = a v1-namespace operation observed in the spike fixture; subtype TBD when decoder lands

For v1-namespace instructions, the discriminator is the first byte and a sub-instruction tag follows. Decoder dispatches on the pair.

For our purpose only the (name, symbol, uri) fields matter. Other variants (Verify, Print, Sign) emit no metadata change and are skipped.

**Token-2022 metadata extension:** stream-decoder filters by program
ID `TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb` and walks the
extension-namespace instructions:

- `TokenMetadataInitialize` (writes name + symbol + uri inline at
  mint creation).
- `TokenMetadataUpdateField` (updates one of {name, symbol, uri}).
- `TokenMetadataUpdateAuthority` (rotates the update authority).

The discriminator scheme is spl-discriminator hashes (8-byte
prefixes derived from instruction names), nested under the
Token-2022 program's extension dispatcher. Exact byte sequences
get pinned against a real mainnet fixture when the decoder is
written.

## Account-list positions (Metaplex)

For `CreateMetadataAccountV3`:

- accounts[0] = metadata PDA
- accounts[1] = mint
- accounts[2] = mint authority
- accounts[3] = payer
- accounts[4] = update_authority
- accounts[5] = system program
- accounts[6] = rent sysvar (optional)

The decoder reads `accounts[1]` for the mint and `accounts[4]` for the update_authority. Pinning exact positions for the other discriminators when the decoder lands.

## What ingest sees vs what gets discarded

Today `parse_edges` reads pre/post balances. Every other instruction
in every transaction is discarded. The Metaplex and Token-2022
metadata writes flow through `getBlock` already; the byte cost is
paid; the decoder is CPU-only.

## Historical-mint coverage gap

Mints created before our ingest window started never produce a
stream-decode event. We accept this as a known gap rather than
build a backfill worker, because:

- The dev/portfolio loop runs `docker compose up -d --build` often,
  which triggers `multichain-state-reset` to wipe ClickHouse. There
  is no accumulated historical state for a backfill worker to
  populate against; we start from a fresh DB nearly every cycle.
- For active tokens (the ones that show up in our edges with any
  meaningful frequency), we observe `UpdateField` / Update writes
  often enough that stream-decode catches them within a few hours
  of running.
- For inactive long-tail mints, the agent narrating with the raw
  pubkey is acceptable; this is exactly the long tail where
  metadata adds least value.

If we ever run continuously for long enough that historical
coverage matters (production deployment, multi-week ingest), a
backfill worker becomes worth building. Until then, drop it.

## Storage shape

```sql
CREATE TABLE multichain.token_metadata (
    mint              String,
    signature         String,
    slot              UInt64,
    block_time        UInt32,
    instruction_idx   UInt16,
    is_inner          Bool,
    program           LowCardinality(String),  -- 'metaplex' | 'token2022'
    op                LowCardinality(String),  -- 'create' | 'update'
    name              String,
    symbol            String,
    uri               String,
    update_authority  String,
    version           UInt64
) ENGINE = ReplacingMergeTree(version)
ORDER BY (mint, signature, instruction_idx, is_inner)
```

Event-stream shape (every write recorded). Current metadata per mint is a query (`ORDER BY slot DESC LIMIT 1`), not a materialized view.

## Out of scope

- Backfill via `getAccountInfo` for pre-existing mints. Intentionally
  not built; see "Historical-mint coverage gap" above.
- Off-chain HTTP fetch of the JSON at the URI. Separate plan.
- Agent primitive `get_token_info(mint)`. Filed once on-chain leg
  lands in ClickHouse.
- Migrating Edge / TokenMetadataEvent to proto wire types. Becomes
  worth doing when a non-Rust consumer needs to read these rows.

## Phase 0 fixtures

Saved for unit-test inputs when the decoder lands:

- **Metaplex update instruction (v1-namespace):** signature `8YKtPWJvEm4Ybc7XnUZYvfibakP24fsZnaWeRsmnfiw6DjogATcNnwbUGzQoQtS7ChVooNmmEjgkYuh6PU8dXkY` slot 418342223, instruction[1], data `D9kCuD4PTuQuyCK` (discriminator 49). Sanity test: decoder ignores it (not a name/symbol/uri write).
- **Metaplex account state:** USDC metadata PDA `5x38Kp4hvdomTCnCrAny4UtMUt5rQBdB6px2K1Ui45Wq`, 679 bytes base64. Borsh decoder must extract `name="USD Coin"`, `symbol="USDC"`, `uri=""`.
- **Token-2022 account state:** PYUSD mint `2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo`. jsonParsed exposes `extensions[].state` directly with `name="PayPal USD"`, `symbol="PYUSD"`, `uri="https://token-metadata.paxos.com/pyusd_metadata/prod/solana/pyusd_metadata.json"`. No decoder needed; backfill reads structured fields.
- **Metaplex `CreateMetadataAccountV3` instruction (disc 33):** not
  pulled yet. Public mainnet RPC throttled the scan; pump.fun's
  recent launches go through their own program rather than direct
  CPI to disc 33. The decoder is shipped on the strength of the
  borsh schema mirroring upstream `mpl-token-metadata`. Real-data
  verification happens once the runner-wiring phase is live and we
  observe the table populating from real mainnet ingest. If we want
  a fixture before then, pull through our own rate-limited backend
  RPC client or via a Helius free-tier endpoint rather than the
  public RPC.
- **Token-2022 `TokenMetadataInitialize` instruction:** not pulled
  yet. Same stance: pin the discriminator and account positions
  against a mainnet fixture when the Token-2022 stream decoder is
  written, ideally through our own rate-limited RPC client.
