//! Per-window analytics task. One spawned per rolling window. Wakes
//! every 3s, takes a brief read lock to snapshot the window's edge
//! view + per-node aggregates, then runs Louvain + MPC scoring + tip
//! detection + MEV searcher profiling + role classification off-lock,
//! and broadcasts the diff against the previous tick.
//!
//! Architecture: snapshot pattern, NOT shadow. The task does not
//! subscribe to ingest events or mirror an adjacency map; it pulls a
//! fresh snapshot at every tick. The lock is held only during
//! `snapshot_window`, never during analytics work.
//!
//! Components below `SUB_CLUSTER_THRESHOLD` skip Louvain entirely
//! (each node gets its own community via the previous-tick id when
//! known, fresh otherwise). Larger components feed through Louvain;
//! the per-component partitions are stitched together with stable
//! global ids before the rest of the analyses run.
use std::sync::Arc;
use std::time::Duration;

use rustc_hash::{FxHashMap, FxHashSet};
use tokio::sync::watch;
use tracing::{debug, warn};

use crate::analytics::delta::{AnalyticsBatch, AnalyticsSnapshot};
use crate::analytics::louvain::louvain_per_component;
use crate::analytics::mev::{build_tips_touched, detect_tip_accounts};
use crate::analytics::mpc::detect_mpc_communities;
use crate::analytics::roles::{NodeRole, classify_nodes};
use crate::analytics::snapshot::snapshot_window;
use crate::analytics::stable_labels::stable_match;
use crate::graph::interner::NodeIdx;
use crate::state::AppState;

/// Tick cadence. Time-bound, no event-driven wakeup.
const TICK: Duration = Duration::from_secs(3);
/// Stagger between the 6 windows' first ticks. Avoids 6 tasks
/// snapshotting simultaneously and contending for the read lock.
const STAGGER: Duration = Duration::from_millis(500);
/// Components smaller than this skip Louvain. Each node still gets a
/// stable community id (passthrough from previous tick when possible).
const SUB_CLUSTER_THRESHOLD: usize = 8;

/// Run the analytics loop for one window. Owns its watch sender +
/// broadcast producer. Loops until shutdown.
pub async fn run(
    window_idx: usize,
    state: AppState,
    snapshot_tx: watch::Sender<Arc<AnalyticsSnapshot>>,
    mut shutdown: watch::Receiver<bool>,
) {
    // Per-window stagger so all 6 tasks don't take the lock at once.
    let initial = STAGGER * window_idx as u32;
    if !initial.is_zero() {
        tokio::select! {
            _ = tokio::time::sleep(initial) => {},
            _ = shutdown.changed() => {
                if *shutdown.borrow() { return; }
            }
        }
    }

    let mut prev_labels: Option<FxHashMap<NodeIdx, u32>> = None;
    let mut prev_roles: Option<FxHashMap<NodeIdx, NodeRole>> = None;
    let mut next_global_id: u32 = 0;
    let mut epoch: u32 = 0;

    loop {
        tokio::select! {
            _ = tokio::time::sleep(TICK) => {}
            _ = shutdown.changed() => {
                if *shutdown.borrow() { break; }
            }
        }

        let snap = {
            let g = state.graph.read();
            snapshot_window(&g, window_idx)
        };

        // Empty window: emit a removals-only batch if we previously had
        // labels or roles, then clear state.
        if snap.adj.is_empty() {
            let prev_label_keys: Vec<u32> = prev_labels
                .as_ref()
                .map(|m| m.keys().copied().collect())
                .unwrap_or_default();
            let prev_role_keys: Vec<u32> = prev_roles
                .as_ref()
                .map(|m| m.keys().copied().collect())
                .unwrap_or_default();

            if prev_label_keys.is_empty() && prev_role_keys.is_empty() {
                continue;
            }

            epoch = epoch.saturating_add(1);
            let batch = Arc::new(AnalyticsBatch {
                epoch,
                community_changes: Vec::new(),
                community_removals: prev_label_keys,
                role_changes: Vec::new(),
                role_removals: prev_role_keys,
            });
            let snapshot = Arc::new(AnalyticsSnapshot {
                epoch,
                labels: FxHashMap::default(),
                roles: FxHashMap::default(),
            });
            let _ = snapshot_tx.send(snapshot);
            let _ = state.analytics.sender(window_idx).send(batch);
            prev_labels = None;
            prev_roles = None;
            continue;
        }

        // === Phase 1: Louvain per component ===
        // Cross-component edges have zero weight, so per-component is
        // mathematically equivalent to global. Stitch into a single
        // partition keyed by NodeIdx, namespaced by component so local
        // ids from different components don't collide.
        let mut local_partition: FxHashMap<NodeIdx, u32> = FxHashMap::default();
        let mut comp_local_offset: u32 = 0;
        let mut sorted_components: Vec<(&u32, &FxHashSet<NodeIdx>)> =
            snap.components.iter().collect();
        // Deterministic iteration order: by component id ascending.
        sorted_components.sort_by_key(|(cid, _)| **cid);

        for (_cid, members) in sorted_components {
            if members.len() < SUB_CLUSTER_THRESHOLD {
                // Tiny component: each node is its own local community.
                for &node in members {
                    local_partition.insert(node, comp_local_offset);
                    comp_local_offset = comp_local_offset.saturating_add(1);
                }
                continue;
            }
            let part = louvain_per_component(members, &snap.adj);
            let max_local = part.values().copied().max().unwrap_or(0);
            for (node, local_id) in part {
                local_partition.insert(node, comp_local_offset + local_id);
            }
            comp_local_offset = comp_local_offset.saturating_add(max_local + 1);
        }

        // Stable global id assignment.
        let global_labels =
            stable_match(&local_partition, prev_labels.as_ref(), &mut next_global_id);

        // === Phase 2: MPC + MEV + role classification ===
        // All read from the same snapshot + the freshly computed
        // community labels.
        let mpc_communities = detect_mpc_communities(&snap, &global_labels);
        let mpc_members: FxHashSet<NodeIdx> = global_labels
            .iter()
            .filter_map(|(&node, &cid)| {
                if mpc_communities.contains(&cid) {
                    Some(node)
                } else {
                    None
                }
            })
            .collect();

        let tips = detect_tip_accounts(&snap);
        let tips_touched = build_tips_touched(&snap, &tips);
        let global_roles = classify_nodes(&snap, &tips, &mpc_members, &tips_touched);

        // === Phase 3: Diff against previous tick to build the batch ===
        epoch = epoch.saturating_add(1);

        let mut community_changes: Vec<(u32, u32)> = Vec::new();
        let mut community_removals: Vec<u32> = Vec::new();
        if let Some(prev) = &prev_labels {
            for (&node, &gid) in &global_labels {
                match prev.get(&node) {
                    Some(&old) if old == gid => {}
                    _ => community_changes.push((node, gid)),
                }
            }
            for (&node, _) in prev {
                if !global_labels.contains_key(&node) {
                    community_removals.push(node);
                }
            }
        } else {
            for (&node, &gid) in &global_labels {
                community_changes.push((node, gid));
            }
        }

        let mut role_changes: Vec<(u32, NodeRole)> = Vec::new();
        let mut role_removals: Vec<u32> = Vec::new();
        if let Some(prev) = &prev_roles {
            for (&node, &role) in &global_roles {
                match prev.get(&node) {
                    Some(&old) if old == role => {}
                    _ => role_changes.push((node, role)),
                }
            }
            for (&node, _) in prev {
                if !global_roles.contains_key(&node) {
                    role_removals.push(node);
                }
            }
        } else {
            for (&node, &role) in &global_roles {
                role_changes.push((node, role));
            }
        }

        let batch = Arc::new(AnalyticsBatch {
            epoch,
            community_changes,
            community_removals,
            role_changes,
            role_removals,
        });
        let snapshot = Arc::new(AnalyticsSnapshot {
            epoch,
            labels: global_labels.clone(),
            roles: global_roles.clone(),
        });

        // Push snapshot first so a brand-new SSE bootstrap sees the
        // newest snapshot consistent with (or one batch ahead of) the
        // broadcast it just subscribed to.
        if snapshot_tx.send(snapshot).is_err() {
            warn!(window = window_idx, "analytics watch closed; exiting");
            break;
        }
        match state.analytics.sender(window_idx).send(batch) {
            Ok(n) => {
                debug!(
                    window = window_idx,
                    epoch,
                    subscribers = n,
                    "analytics batch broadcast"
                );
            }
            Err(_) => {
                // No subscribers right now; that's fine.
            }
        }

        prev_labels = Some(global_labels);
        prev_roles = Some(global_roles);
    }
}
