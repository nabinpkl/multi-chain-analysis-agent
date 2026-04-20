use std::collections::{HashMap, HashSet, VecDeque};
use std::time::{SystemTime, UNIX_EPOCH};

use axum::Json;
use axum::extract::{Query, State};
use axum::http::StatusCode;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::domain::{
    EdgeAggregate, EdgeView, LAMPORTS_PER_SOL, NodeView, OverviewResponse, StatsView,
    WalletAggregate, WindowStats, WindowView,
};
use crate::overview_cache::CacheKey;
use crate::state::AppState;

const DEFAULT_WINDOW: &str = "24h";
const DEFAULT_EDGE_LIMIT: u32 = 500;
const DEFAULT_WHALE_PAD: u32 = 50;
const MIN_EDGE_LIMIT: u32 = 100;
const MAX_EDGE_LIMIT: u32 = 1000;
const MAX_WHALE_PAD: u32 = 200;

#[derive(Deserialize)]
pub struct OverviewParams {
    window: Option<String>,
    edge_limit: Option<u32>,
    whale_pad: Option<u32>,
}

pub async fn overview(
    State(state): State<AppState>,
    Query(params): Query<OverviewParams>,
) -> Result<Json<OverviewResponse>, (StatusCode, Json<Value>)> {
    let window_label = params.window.as_deref().unwrap_or(DEFAULT_WINDOW);
    let window_secs = parse_window(window_label).ok_or_else(|| {
        (
            StatusCode::BAD_REQUEST,
            Json(json!({
                "error": "invalid window",
                "allowed": ["15m", "1h", "6h", "24h"]
            })),
        )
    })?;
    let window_static: &'static str = match window_label {
        "15m" => "15m",
        "1h" => "1h",
        "6h" => "6h",
        _ => "24h",
    };

    let edge_limit = params
        .edge_limit
        .unwrap_or(DEFAULT_EDGE_LIMIT)
        .clamp(MIN_EDGE_LIMIT, MAX_EDGE_LIMIT);
    let whale_pad = params.whale_pad.unwrap_or(DEFAULT_WHALE_PAD).min(MAX_WHALE_PAD);

    let key = CacheKey {
        window_label: window_static,
        edge_limit,
        whale_pad,
    };

    let graph = state.graph.clone();
    let ttl = state.overview_cache.ttl_secs();
    let resp = state
        .overview_cache
        .get_or_compute(key, || async move {
            compute_overview(graph.as_ref(), window_static, window_secs, edge_limit, whale_pad, ttl).await
        })
        .await
        .map_err(|e| {
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({ "error": e.to_string() })),
            )
        })?;

    Ok(Json((*resp).clone()))
}

fn parse_window(label: &str) -> Option<u32> {
    match label {
        "15m" => Some(15 * 60),
        "1h" => Some(60 * 60),
        "6h" => Some(6 * 60 * 60),
        "24h" => Some(24 * 60 * 60),
        _ => None,
    }
}

async fn compute_overview(
    graph: &dyn crate::store::GraphStore,
    window_label: &str,
    window_secs: u32,
    edge_limit: u32,
    whale_pad: u32,
    ttl_secs: u32,
) -> anyhow::Result<OverviewResponse> {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as u32)
        .unwrap_or(0);
    let from_ts = now.saturating_sub(window_secs);
    let to_ts = now;

    let wallet_query_limit = (edge_limit * 4).max(whale_pad * 2).max(200);

    let (edges_raw, wallets_raw, stats_raw) = tokio::try_join!(
        graph.top_edges(from_ts, to_ts, edge_limit),
        graph.top_wallets(from_ts, to_ts, wallet_query_limit),
        graph.window_stats(from_ts, to_ts),
    )?;

    let wallet_volume: HashMap<&str, u64> = wallets_raw
        .iter()
        .map(|w| (w.wallet.as_str(), w.volume_lamports))
        .collect();

    let (node_views, edge_views) = build_graph(&edges_raw, &wallet_volume, &wallets_raw, whale_pad);

    let stats = build_stats(&stats_raw, &wallets_raw);

    Ok(OverviewResponse {
        window: WindowView {
            from: from_ts,
            to: to_ts,
            label: window_label.to_string(),
        },
        stats,
        nodes: node_views,
        edges: edge_views,
        generated_at: now,
        cache_ttl_secs: ttl_secs,
    })
}

fn build_graph(
    edges: &[EdgeAggregate],
    wallet_volume: &HashMap<&str, u64>,
    wallets_sorted: &[WalletAggregate],
    whale_pad: u32,
) -> (Vec<NodeView>, Vec<EdgeView>) {
    let mut edge_views = Vec::with_capacity(edges.len());
    let mut in_edges: HashSet<&str> = HashSet::new();
    let mut adjacency: HashMap<&str, Vec<&str>> = HashMap::new();

    for e in edges {
        in_edges.insert(e.from_wallet.as_str());
        in_edges.insert(e.to_wallet.as_str());
        adjacency
            .entry(e.from_wallet.as_str())
            .or_default()
            .push(e.to_wallet.as_str());
        adjacency
            .entry(e.to_wallet.as_str())
            .or_default()
            .push(e.from_wallet.as_str());
        edge_views.push(EdgeView {
            from: e.from_wallet.clone(),
            to: e.to_wallet.clone(),
            volume_sol: lamports_to_sol(e.volume_lamports),
            tx_count: e.tx_count,
        });
    }

    let component_map = connected_components(&in_edges, &adjacency);

    let mut node_views = Vec::with_capacity(in_edges.len() + whale_pad as usize);
    for wallet in &in_edges {
        node_views.push(NodeView {
            id: (*wallet).to_string(),
            volume_sol: lamports_to_sol(*wallet_volume.get(wallet).unwrap_or(&0)),
            component: component_map.get(wallet).copied(),
        });
    }

    let mut pads_added = 0u32;
    for w in wallets_sorted {
        if pads_added >= whale_pad {
            break;
        }
        if in_edges.contains(w.wallet.as_str()) {
            continue;
        }
        node_views.push(NodeView {
            id: w.wallet.clone(),
            volume_sol: lamports_to_sol(w.volume_lamports),
            component: None,
        });
        pads_added += 1;
    }

    (node_views, edge_views)
}

/// Connected components with content-addressed ids.
///
/// `HashSet` iteration is non-deterministic in Rust, so naive BFS would
/// hand out different ids for identical graphs across calls. Here we:
///
///   1. BFS over sorted start nodes (and sorted neighbors) so group membership
///      is found deterministically.
///   2. Sort each group's members, then sort the groups by their smallest
///      member, and assign ids `0..N` by that rank.
///
/// Two identical graphs therefore always produce identical ids, which lets
/// the frontend map `component_id -> color` without the color flip-flopping
/// every poll.
fn connected_components<'a>(
    nodes: &HashSet<&'a str>,
    adjacency: &HashMap<&'a str, Vec<&'a str>>,
) -> HashMap<&'a str, u32> {
    let mut sorted_nodes: Vec<&str> = nodes.iter().copied().collect();
    sorted_nodes.sort_unstable();

    let mut temp_id_of: HashMap<&str, usize> = HashMap::with_capacity(nodes.len());
    let mut groups: Vec<Vec<&str>> = Vec::new();

    for &start in &sorted_nodes {
        if temp_id_of.contains_key(start) {
            continue;
        }
        let id = groups.len();
        let mut members: Vec<&str> = Vec::new();
        let mut queue: VecDeque<&str> = VecDeque::from([start]);
        temp_id_of.insert(start, id);
        members.push(start);
        while let Some(node) = queue.pop_front() {
            if let Some(neighbors) = adjacency.get(node) {
                let mut sorted_neighbors: Vec<&str> = neighbors.iter().copied().collect();
                sorted_neighbors.sort_unstable();
                for n in sorted_neighbors {
                    if !temp_id_of.contains_key(n) {
                        temp_id_of.insert(n, id);
                        members.push(n);
                        queue.push_back(n);
                    }
                }
            }
        }
        members.sort_unstable();
        groups.push(members);
    }

    let mut order: Vec<usize> = (0..groups.len()).collect();
    order.sort_unstable_by(|&a, &b| groups[a][0].cmp(groups[b][0]));

    let mut final_id_of_temp: HashMap<usize, u32> = HashMap::with_capacity(groups.len());
    for (rank, temp_id) in order.iter().enumerate() {
        final_id_of_temp.insert(*temp_id, rank as u32);
    }

    temp_id_of
        .into_iter()
        .map(|(wallet, temp_id)| (wallet, final_id_of_temp[&temp_id]))
        .collect()
}

fn build_stats(stats: &WindowStats, wallets_sorted: &[WalletAggregate]) -> StatsView {
    let (top_wallet, top_wallet_volume_sol) = wallets_sorted
        .first()
        .map(|w| (Some(w.wallet.clone()), Some(lamports_to_sol(w.volume_lamports))))
        .unwrap_or((None, None));

    StatsView {
        total_volume_sol: lamports_to_sol(stats.total_volume_lamports),
        total_txs: stats.total_txs,
        unique_wallets: stats.unique_wallets,
        top_wallet,
        top_wallet_volume_sol,
    }
}

fn lamports_to_sol(lamports: u64) -> f64 {
    lamports as f64 / LAMPORTS_PER_SOL
}
