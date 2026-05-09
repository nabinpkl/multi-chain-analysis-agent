//! On-demand token-metadata lookup with lazy ClickHouse-backed cache.
//!
//! When the agent encounters a mint whose metadata isn't already in
//! `multichain.token_metadata`, `fetch_token_metadata` resolves the
//! on-chain state via `getAccountInfo`. One RPC call per first-time-
//! asked mint; subsequent calls within the configured TTL window
//! (`METADATA_CACHE_TTL_SLOTS`, default ~1 hour) are served from CH
//! without touching RPC.
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
//! The cache here is a stopgap that bounds staleness during the gap
//! before issue #48 (CDC instruction decoding) lands. After CDC, the
//! TTL refresh path becomes dead code; the cache is kept fresh by
//! ingest-time writes driven by `UpdateMetadataAccountV2` /
//! `TokenMetadataUpdateField` instruction decoding.

pub mod cache;
pub mod fetch;
