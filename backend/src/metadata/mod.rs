//! On-demand token-metadata lookup.
//!
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
//! This is the only path that populates `multichain.token_metadata`. A
//! prior streaming `getBlock` decode of Metaplex Create instructions was
//! removed once empirical mainnet sampling showed it caught very little:
//! most active mints (USDC, BONK, JUP) predate any ingest window, and
//! current "create" activity has shifted to programs whose metadata
//! `getAccountInfo` jsonParsed already structures for us. See
//! `docs/architecture/token-metadata-ingestion.md` for the full story.

pub mod fetch;
