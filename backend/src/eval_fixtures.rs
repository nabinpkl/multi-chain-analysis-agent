//! Eval-driven fixture store. Holds canned `get_token_info`
//! responses keyed by mint pubkey. Populated by the agent service via
//! HTTP for adversarial-mint eval cases; consulted by
//! `primitives::get_token_info::compute` before it hits the live
//! metadata fetcher.
//!
//! Concurrency contract: single-eval-at-a-time. The eval CLI runs
//! cases sequentially, posts fixtures before each case and clears
//! them after, so the global mint-keyed store is the right shape:
//! one writer, one reader, no overlap. Parallel eval execution would
//! race; if that ever lands, the keying must move to per-snapshot
//! or per-MCP-session. Today the runner's serial behavior is the
//! contract.
//!
//! Production safety: the registration endpoint
//! (`POST /eval/fixtures`) and the clear endpoint
//! (`DELETE /eval/fixtures`) live on the internal-only router and
//! are gated by the `BACKEND_ENABLE_EVAL_FIXTURES` env flag in
//! `state::AppState`. Default off in production; the docker compose
//! file flips it on for the dev/eval profile.

use std::sync::Arc;

use dashmap::DashMap;
use serde::{Deserialize, Serialize};

use crate::canonical_mints;
use crate::state::AppState;

/// One fixture entry. Mirrors the populatable fields of
/// `GetTokenInfoOutput` minus the verification stamp (the stamp is
/// applied server-side from the mint pubkey, never trusted from the
/// fixture author).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TokenInfoFixture {
    pub mint: String,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub symbol: Option<String>,
    #[serde(default)]
    pub uri: Option<String>,
    #[serde(default)]
    pub update_authority: Option<String>,
    /// `"metaplex"` | `"token2022"` | `""`. Defaults to `"token2022"`
    /// because that's the natural shape of an impostor (Token-2022
    /// is the minting path attackers reach for when impersonating a
    /// canonical SPL token).
    #[serde(default = "default_source_program")]
    pub source_program: String,
}

fn default_source_program() -> String {
    "token2022".to_string()
}

/// Wire shape for `POST /eval/fixtures`. Keyed by primitive name so
/// the same envelope extends to future fixtures (e.g. wallet_profile)
/// without breaking existing eval cases.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct RegisterRequest {
    #[serde(default)]
    pub get_token_info: Vec<TokenInfoFixture>,
}

/// Live store. Held by `AppState.eval_fixtures`; cloning is cheap
/// (Arc bump).
pub type Store = Arc<DashMap<String, TokenInfoFixture>>;

pub fn empty_store() -> Store {
    Arc::new(DashMap::new())
}

/// Replace the live store contents with `req`. Returns the number of
/// entries written. Rejects an entry pointing at a canonical mint
/// pubkey: impostors live at non-canonical pubkeys by definition, and
/// allowing a fixture to override a canonical mint would let an eval
/// case test impersonation of a pubkey we DO stand behind, which is
/// the wrong threat model.
pub fn replace(state: &AppState, req: RegisterRequest) -> Result<usize, String> {
    if !state.eval_fixtures_enabled {
        return Err(
            "eval-fixture endpoints are disabled (BACKEND_ENABLE_EVAL_FIXTURES is off)"
                .to_string(),
        );
    }
    for entry in &req.get_token_info {
        if entry.mint.trim().is_empty() {
            return Err("get_token_info fixture has empty mint pubkey".to_string());
        }
        if canonical_mints::lookup(&entry.mint).is_some() {
            return Err(format!(
                "fixture targets canonical mint pubkey {} (cannot impersonate a verified mint)",
                entry.mint
            ));
        }
    }
    state.eval_fixtures.clear();
    let count = req.get_token_info.len();
    for entry in req.get_token_info {
        state.eval_fixtures.insert(entry.mint.clone(), entry);
    }
    Ok(count)
}

/// Drop every fixture entry. Always succeeds (clearing an already-
/// empty store is a no-op).
pub fn clear(state: &AppState) -> Result<(), String> {
    if !state.eval_fixtures_enabled {
        return Err(
            "eval-fixture endpoints are disabled (BACKEND_ENABLE_EVAL_FIXTURES is off)"
                .to_string(),
        );
    }
    state.eval_fixtures.clear();
    Ok(())
}

/// Look up a fixture for the given mint pubkey. Returns the cloned
/// entry on hit so the caller can mutate freely; returns `None` when
/// the store is empty or the mint isn't registered.
pub fn lookup(state: &AppState, mint: &str) -> Option<TokenInfoFixture> {
    state.eval_fixtures.get(mint).map(|r| r.clone())
}

#[cfg(test)]
mod tests {
    use super::*;

    const IMPOSTOR_PUBKEY: &str = "5xv9pkS5kFx7VRSMxpzpL1uYnPJyxXqkUcQNoZ8MnHvW";
    const CANONICAL_USDC: &str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";

    fn impostor_entry() -> TokenInfoFixture {
        TokenInfoFixture {
            mint: IMPOSTOR_PUBKEY.to_string(),
            name: Some("USD Coin".to_string()),
            symbol: Some("USDC".to_string()),
            uri: Some(String::new()),
            update_authority: Some(IMPOSTOR_PUBKEY.to_string()),
            source_program: "token2022".to_string(),
        }
    }

    fn make_state_with_flag(enabled: bool) -> AppState {
        AppState::test_stub_with_eval_fixtures(enabled)
    }

    #[test]
    fn replace_then_lookup_round_trip() {
        let state = make_state_with_flag(true);
        let req = RegisterRequest {
            get_token_info: vec![impostor_entry()],
        };
        let count = replace(&state, req).expect("replace ok");
        assert_eq!(count, 1);
        let hit = lookup(&state, IMPOSTOR_PUBKEY).expect("fixture present");
        assert_eq!(hit.symbol.as_deref(), Some("USDC"));
    }

    #[test]
    fn replace_clears_old_entries() {
        let state = make_state_with_flag(true);
        replace(
            &state,
            RegisterRequest {
                get_token_info: vec![impostor_entry()],
            },
        )
        .unwrap();
        // Second replace with empty list drops the old entry.
        replace(&state, RegisterRequest::default()).unwrap();
        assert!(lookup(&state, IMPOSTOR_PUBKEY).is_none());
    }

    #[test]
    fn clear_drops_everything() {
        let state = make_state_with_flag(true);
        replace(
            &state,
            RegisterRequest {
                get_token_info: vec![impostor_entry()],
            },
        )
        .unwrap();
        clear(&state).unwrap();
        assert!(lookup(&state, IMPOSTOR_PUBKEY).is_none());
    }

    #[test]
    fn rejects_canonical_mint_target() {
        let state = make_state_with_flag(true);
        let mut entry = impostor_entry();
        entry.mint = CANONICAL_USDC.to_string();
        let err = replace(
            &state,
            RegisterRequest {
                get_token_info: vec![entry],
            },
        )
        .expect_err("must reject canonical mint");
        assert!(err.contains("canonical"));
    }

    #[test]
    fn rejects_empty_mint() {
        let state = make_state_with_flag(true);
        let mut entry = impostor_entry();
        entry.mint = String::new();
        let err = replace(
            &state,
            RegisterRequest {
                get_token_info: vec![entry],
            },
        )
        .expect_err("must reject empty mint");
        assert!(err.contains("empty"));
    }

    #[test]
    fn refuses_when_flag_off() {
        let state = make_state_with_flag(false);
        let err = replace(
            &state,
            RegisterRequest {
                get_token_info: vec![impostor_entry()],
            },
        )
        .expect_err("must refuse when feature flag off");
        assert!(err.contains("disabled"));
        let err2 = clear(&state).expect_err("clear also refused");
        assert!(err2.contains("disabled"));
    }

    #[test]
    fn lookup_empty_store_returns_none() {
        let state = make_state_with_flag(true);
        assert!(lookup(&state, IMPOSTOR_PUBKEY).is_none());
    }
}
