//! On-demand token-metadata lookup.
//!
//! Counterpart to `ingest::metadata`, which decodes Metaplex Create
//! instructions inside the streaming `getBlock` ingest path. This
//! module handles the OTHER access pattern: agent-time lazy fetch.
//! When the agent encounters a mint whose metadata isn't already in
//! `multichain.token_metadata`, it calls `fetch_token_metadata` to
//! resolve the on-chain state via `getAccountInfo`. One RPC call per
//! first-time-asked mint, cached forever in ClickHouse afterwards.
//!
//! Two source programs are supported:
//!
//! - **Metaplex Token Metadata** (`metaqbxx...`): metadata lives in a
//!   PDA derived from the mint. We compute the PDA, fetch the account,
//!   borsh-decode the prefix.
//! - **SPL Token-2022** (`Tokenz...`): metadata is inline on the mint
//!   account itself as a TLV extension. The RPC's `jsonParsed` encoding
//!   already structures it for us; no borsh needed.
//!
//! Why on-demand rather than stream-decode? Empirical investigation
//! during the metadata pipeline build (see
//! `docs/architecture/token-metadata-ingestion.md` and the multi-hop
//! injection study doc) showed:
//!
//! - Most active mints (USDC, BONK, JUP, etc.) were created BEFORE the
//!   ingest window; stream-decode never sees them.
//! - Most current mainnet "create" activity is pump.fun memecoins via
//!   Token-2022; stream-decode for that path adds zero value over
//!   `getAccountInfo` jsonParsed (which already returns structured
//!   metadata).
//! - The agent only cares about mints it actually encounters in edges;
//!   "fetch on demand" is exactly the right shape.

pub mod fetch;
