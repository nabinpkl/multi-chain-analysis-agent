//! `get_token_info` primitive: resolve a mint pubkey to its on-chain
//! `name / symbol / uri` via the lazy ClickHouse-backed metadata
//! cache. Stateless per call (no snapshot lookup).
//!
//! Logic was inlined in `api::primitives::get_token_info_route` until
//! the MCP tool surface needed the same compute path. Extracting to a
//! free function keeps both consumers (the existing `/primitive/*`
//! HTTP route and the new MCP `get_token_info` tool in
//! `crate::mcp`) calling one place rather than duplicating the RPC +
//! cache wiring. The HTTP route still owns its proto-bridging; this
//! module returns a serde-shaped result that both consumers can map
//! to their respective output types.

use serde::{Deserialize, Serialize};
use solana_pubkey::Pubkey;
use thiserror::Error;

use crate::metadata;
use crate::state::AppState;

/// Resolved token metadata. Mirrors the shape of
/// `proto::GetTokenInfoOutput` (the wire type the HTTP route returns)
/// minus the proto-only `..Default::default()` ergonomic. None on
/// `name / symbol / uri / update_authority` means "mint exists on
/// chain but has no metadata via either the Metaplex PDA or the
/// Token-2022 extension"; `source_program` is empty in that case.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GetTokenInfoOutput {
    pub mint: String,
    pub name: Option<String>,
    pub symbol: Option<String>,
    pub uri: Option<String>,
    pub update_authority: Option<String>,
    /// `"metaplex"` or `"token2022"` when metadata was found,
    /// empty string otherwise.
    pub source_program: String,
}

#[derive(Debug, Error)]
pub enum GetTokenInfoError {
    #[error("invalid mint pubkey: {0}")]
    InvalidMint(String),
    #[error("RPC is not configured (SOLANA_RPC_URL is unset); get_token_info needs RPC access")]
    RpcDisabled,
    #[error("RPC fetch failed: {0}")]
    RpcError(String),
}

/// Compute `get_token_info` for one mint. Trims and validates the
/// pubkey, builds a `CacheCtx` from `AppState`, calls the cache-aware
/// fetcher, and shapes the result into `GetTokenInfoOutput`.
pub async fn compute(state: &AppState, mint_b58: &str) -> Result<GetTokenInfoOutput, GetTokenInfoError> {
    let mint_b58 = mint_b58.trim().to_string();
    if mint_b58.is_empty() {
        return Err(GetTokenInfoError::InvalidMint("mint pubkey is empty".into()));
    }
    let mint_pk = parse_pubkey(&mint_b58).map_err(GetTokenInfoError::InvalidMint)?;

    let rpc = state.rpc.clone().ok_or(GetTokenInfoError::RpcDisabled)?;

    let cache_ctx = metadata::fetch::CacheCtx {
        clickhouse: &state.clickhouse,
        // Tip-unknown sentinel = 0; cache::read_cached treats every row
        // as stale until the first `getSlot` round-trip lands.
        current_slot: state.tip.current().unwrap_or(0),
        ttl_slots: state.metadata_cache_ttl_slots,
    };
    let metadata_opt = metadata::fetch::fetch_token_metadata(&rpc, &mint_pk, &cache_ctx)
        .await
        .map_err(|e| GetTokenInfoError::RpcError(format!("getAccountInfo failed: {e}")))?;

    Ok(match metadata_opt {
        Some(meta) => GetTokenInfoOutput {
            mint: mint_b58,
            name: Some(meta.name),
            symbol: Some(meta.symbol),
            uri: Some(meta.uri),
            update_authority: Some(meta.update_authority),
            source_program: meta.program.to_string(),
        },
        None => GetTokenInfoOutput {
            mint: mint_b58,
            name: None,
            symbol: None,
            uri: None,
            update_authority: None,
            source_program: String::new(),
        },
    })
}

fn parse_pubkey(s: &str) -> Result<Pubkey, String> {
    let mut bytes = [0u8; 32];
    bs58::decode(s)
        .onto(&mut bytes[..])
        .map_err(|e| format!("invalid base58 pubkey: {e}"))?;
    Ok(Pubkey::new_from_array(bytes))
}
