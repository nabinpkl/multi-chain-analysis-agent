//! Canonical-mint registry. Mirror of the (now-removed)
//! `agent_service.canonical_mints` Python module. Owning the registry
//! in Rust makes the verification stamp single-source-of-truth across
//! both runtimes: codex (via the MCP `get_token_info` tool) and
//! pydantic-ai (via the HTTP `/primitive/get_token_info` route) both
//! call into `crate::primitives::get_token_info::compute`, which
//! applies `stamp_verification` before returning. The proto fields
//! `verified`, `canonical_name`, and `canonical_symbol` carry the
//! stamp across the wire to both Python consumers.
//!
//! Solana SPL transfers carry the mint pubkey directly  every token
//! account is mint-pinned at creation and every Transfer /
//! TransferChecked references the pubkey, never a symbol. The pubkey
//! is the forge-proof identifier. The on-chain `name` / `symbol` /
//! `uri` (Metaplex or Token-2022 metadata) are attacker-controlled
//! strings: anyone can create a mint with `name="USD Coin"` and
//! `symbol="USDC"`.
//!
//! This registry maps the canonical pubkey for a small set of
//! blue-chip tokens to display strings we control. `stamp_verification`
//! sets `verified: bool` plus optional `canonical_name` /
//! `canonical_symbol` on the `GetTokenInfoOutput` payload. The system
//! prompt teaches the model to prefer canonical fields when verified
//! and to qualify the on-chain symbol as unverified otherwise.
//!
//! The on-chain `name` / `symbol` / `uri` pass through unchanged so
//! the model retains the forensic surface; the verified flag is a
//! tag, not a filter.
//!
//! Curation policy:
//! - Add entries by PR review only. This is a domain constant, not
//!   env config; the canonical USDC mint is the same in every
//!   deployment.
//! - Keep the set small. The point is "we have stood behind the
//!   identity of this pubkey," not "we have heard of this token."
//! - LSTs (JitoSOL, mSOL, bSOL) and majors (JUP, BONK, PYTH, WIF)
//!   are intentionally deferred until an eval shows a concrete
//!   narrative-quality miss on one of them.

use std::collections::HashMap;
use std::sync::OnceLock;

use crate::primitives::get_token_info::GetTokenInfoOutput;

/// One row in the canonical-mint registry.
#[derive(Debug, Clone)]
pub struct CanonicalToken {
    pub mint: &'static str,
    pub canonical_name: &'static str,
    pub canonical_symbol: &'static str,
}

const REGISTRY_ENTRIES: &[CanonicalToken] = &[
    CanonicalToken {
        mint: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        canonical_name: "USD Coin",
        canonical_symbol: "USDC",
    },
    CanonicalToken {
        mint: "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        canonical_name: "Tether USD",
        canonical_symbol: "USDT",
    },
    CanonicalToken {
        mint: "So11111111111111111111111111111111111111112",
        canonical_name: "Wrapped SOL",
        canonical_symbol: "wSOL",
    },
];

fn registry() -> &'static HashMap<&'static str, &'static CanonicalToken> {
    static CACHE: OnceLock<HashMap<&'static str, &'static CanonicalToken>> = OnceLock::new();
    CACHE.get_or_init(|| {
        REGISTRY_ENTRIES
            .iter()
            .map(|entry| (entry.mint, entry))
            .collect()
    })
}

/// Look up the canonical record for `mint`. Returns `None` for any
/// pubkey not in the registry.
pub fn lookup(mint: &str) -> Option<&'static CanonicalToken> {
    registry().get(mint).copied()
}

/// Stamp `verified` plus optional canonical_* fields on a
/// `GetTokenInfoOutput` in place. When the mint is in the registry,
/// `verified` is set to true and the canonical_* fields are populated
/// from the registry entry. When the mint is unknown, `verified` is
/// set to false and the canonical_* fields stay `None`.
///
/// The on-chain `name` / `symbol` / `uri` / `update_authority` /
/// `source_program` fields are left untouched. The model still sees
/// them as forensic surface; the `verified` flag is the discriminator
/// the prompt's `token_verification` rule uses to decide whether the
/// symbol can be narrated bare or must be qualified.
pub fn stamp_verification(out: &mut GetTokenInfoOutput) {
    match lookup(&out.mint) {
        Some(token) => {
            out.verified = true;
            out.canonical_name = Some(token.canonical_name.to_string());
            out.canonical_symbol = Some(token.canonical_symbol.to_string());
        }
        None => {
            out.verified = false;
            out.canonical_name = None;
            out.canonical_symbol = None;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const CANONICAL_USDC: &str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
    // Deliberately non-registry pubkey, mirroring the Python test
    // suite's `_IMPOSTOR_PUBKEY` so a human can cross-reference.
    const IMPOSTOR_PUBKEY: &str = "5xv9pkS5kFx7VRSMxpzpL1uYnPJyxXqkUcQNoZ8MnHvW";

    fn payload(mint: &str, name: &str, symbol: &str) -> GetTokenInfoOutput {
        GetTokenInfoOutput {
            mint: mint.to_string(),
            name: Some(name.to_string()),
            symbol: Some(symbol.to_string()),
            uri: Some(String::new()),
            update_authority: Some(String::new()),
            source_program: "token2022".to_string(),
            verified: false,
            canonical_name: None,
            canonical_symbol: None,
        }
    }

    #[test]
    fn lookup_canonical_returns_record() {
        let token = lookup(CANONICAL_USDC).expect("canonical USDC present");
        assert_eq!(token.canonical_symbol, "USDC");
        assert_eq!(token.canonical_name, "USD Coin");
    }

    #[test]
    fn lookup_impostor_returns_none() {
        assert!(lookup(IMPOSTOR_PUBKEY).is_none());
    }

    #[test]
    fn stamp_canonical_mint() {
        let mut p = payload(CANONICAL_USDC, "USD Coin", "USDC");
        stamp_verification(&mut p);
        assert!(p.verified);
        assert_eq!(p.canonical_name.as_deref(), Some("USD Coin"));
        assert_eq!(p.canonical_symbol.as_deref(), Some("USDC"));
        // On-chain fields pass through.
        assert_eq!(p.name.as_deref(), Some("USD Coin"));
        assert_eq!(p.symbol.as_deref(), Some("USDC"));
    }

    #[test]
    fn stamp_impostor_mint() {
        let mut p = payload(IMPOSTOR_PUBKEY, "USD Coin", "USDC");
        stamp_verification(&mut p);
        assert!(!p.verified);
        assert!(p.canonical_name.is_none());
        assert!(p.canonical_symbol.is_none());
        // Attacker-controlled fields survive: the model still sees
        // them, just with `verified=false` so the prompt rule
        // qualifies the mention.
        assert_eq!(p.name.as_deref(), Some("USD Coin"));
        assert_eq!(p.symbol.as_deref(), Some("USDC"));
    }

    #[test]
    fn stamp_is_idempotent() {
        let mut p = payload(CANONICAL_USDC, "USD Coin", "USDC");
        stamp_verification(&mut p);
        let first = (p.verified, p.canonical_name.clone(), p.canonical_symbol.clone());
        stamp_verification(&mut p);
        let second = (p.verified, p.canonical_name.clone(), p.canonical_symbol.clone());
        assert_eq!(first, second);
    }

    #[test]
    fn stamp_clears_stale_canonical_when_pubkey_now_unverified() {
        // Synthesize a pre-stamped payload that claims canonical
        // status for a mint that's actually impostor. stamp_verification
        // must overwrite the stale fields, not preserve them.
        let mut p = GetTokenInfoOutput {
            mint: IMPOSTOR_PUBKEY.to_string(),
            name: Some("USD Coin".into()),
            symbol: Some("USDC".into()),
            uri: Some(String::new()),
            update_authority: Some(String::new()),
            source_program: "token2022".into(),
            verified: true,
            canonical_name: Some("Stale Canonical".into()),
            canonical_symbol: Some("STALE".into()),
        };
        stamp_verification(&mut p);
        assert!(!p.verified);
        assert!(p.canonical_name.is_none());
        assert!(p.canonical_symbol.is_none());
    }
}
