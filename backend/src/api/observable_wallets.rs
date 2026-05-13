//! `GET /graph/observable_wallets` — return the top-degree wallets
//! currently visible in the live window.
//!
//! Motivation: eval cases for `wallet_profile`-shaped suites pin a
//! specific wallet address that's "observable in the live window"
//! (recently transferring SOL/SPL). The live window rolls forward,
//! so any pinned wallet eventually ages out and the suite starts
//! failing on "wallet not in current live window" through no fault
//! of the agent code. This endpoint exposes the inverse query: give
//! me a wallet that IS currently observable, so the eval harness can
//! pick one on demand instead of hard-coding an address that
//! bit-rots.
//!
//! Read-only. Same window-derivation logic as `graph_stats`
//! (`api/graph_stats.rs`): edges are filtered to `block_time >
//! latest - window_secs`, visible nodes are the union of edge
//! endpoints, degrees are computed from edge endpoint counts inside
//! the window.

use std::collections::HashMap;

use axum::Json;
use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use rustc_hash::FxHashMap;
use serde::Serialize;
use ts_rs::TS;

use crate::graph::interner::NodeIdx;
use crate::graph::window::parse_window_param;
use crate::state::AppState;

/// Sanity cap on `limit`. The endpoint is intended for "give me a
/// handful of candidates" use, not for browser-side dumps. A higher
/// cap would risk turning this into a graph-export side door.
const MAX_LIMIT: usize = 50;
const DEFAULT_LIMIT: usize = 5;

#[derive(Serialize, TS)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct ObservableWallet {
    /// Base58 pubkey of the wallet.
    pub addr: String,
    /// Degree inside the requested window (count of edges where this
    /// wallet is either endpoint).
    pub degree_in_window: u32,
}

#[derive(Serialize, TS)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct ObservableWalletsResponse {
    pub window_secs: u64,
    pub latest_block_time: u64,
    /// Top-N wallets by `degree_in_window`, descending. Empty when
    /// the window is empty (graph just started up, all live edges
    /// expired, etc).
    pub wallets: Vec<ObservableWallet>,
}

pub async fn observable_wallets(
    State(state): State<AppState>,
    Query(params): Query<HashMap<String, String>>,
) -> Response {
    let window_idx = match parse_window_param(params.get("window").map(|s| s.as_str())) {
        Ok(w) => w,
        Err(msg) => return (StatusCode::BAD_REQUEST, msg).into_response(),
    };
    let window_secs = crate::graph::window::WINDOWS[window_idx];

    let limit = params
        .get("limit")
        .and_then(|s| s.parse::<usize>().ok())
        .unwrap_or(DEFAULT_LIMIT)
        .clamp(1, MAX_LIMIT);

    let graph = state.graph.read();
    let cutoff = if window_idx == crate::graph::window::MAX_WINDOW_IDX {
        0
    } else {
        graph.latest_block_time().saturating_sub(window_secs)
    };

    // Walk live edges restricted to the window. Per-node degree is
    // simply how many in-window edges touch the node as either
    // endpoint. Mirroring `graph_stats`'s edge walk keeps the
    // visibility semantics aligned: the same edges that count toward
    // `total_edges` also count toward each node's degree here.
    let mut degree: FxHashMap<NodeIdx, u32> = FxHashMap::default();
    for slot in graph.edges.iter() {
        let Some(e) = slot.edge.as_ref() else { continue };
        if e.block_time < cutoff {
            continue;
        }
        *degree.entry(e.src).or_insert(0) += 1;
        *degree.entry(e.dst).or_insert(0) += 1;
    }

    // Top-N by degree. `select_nth_unstable_by_key` would be O(N) but
    // we already have to sort the survivor slice anyway to return in
    // ranked order; one full sort over the truncated set is fine at
    // current scale (live-window node count is ~1k-10k).
    let mut ranked: Vec<(NodeIdx, u32)> = degree.into_iter().collect();
    ranked.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    ranked.truncate(limit);

    let wallets: Vec<ObservableWallet> = ranked
        .into_iter()
        .filter_map(|(idx, deg)| {
            graph
                .lookup_pubkey(idx)
                .map(|pk| ObservableWallet {
                    addr: pk.to_string(),
                    degree_in_window: deg,
                })
        })
        .collect();

    Json(ObservableWalletsResponse {
        window_secs,
        latest_block_time: graph.latest_block_time(),
        wallets,
    })
    .into_response()
}
