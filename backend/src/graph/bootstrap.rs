/// Cold-start bootstrap: serialize the slice of GraphState visible to a
/// given rolling window as a stream of synthetic GraphDelta values. All
/// events get seq=0 (no live seq).
///
/// Order:
/// 1. NodeAdded for each node with at least one edge in the window
/// 2. EdgeAdded for each edge whose `block_time` is within the window
///
/// Connectivity is not shipped: the frontend recomputes it from the
/// edge stream so its component view stays window-pure (a global
/// component id can group nodes that aren't connected within a
/// smaller window's edge subset).
use rustc_hash::FxHashSet;

use super::GraphState;
use super::delta::GraphDelta;
use super::interner::NodeIdx;
use super::window::{MAX_WINDOW_IDX, WINDOWS};

/// Produce all bootstrap events for the current state at window `w`.
/// `w` must be a valid window index (0..NUM_WINDOWS).
pub fn bootstrap_events(gs: &GraphState, window_idx: usize) -> Vec<GraphDelta> {
    let cutoff = if window_idx == MAX_WINDOW_IDX {
        // Largest window == global retention; any live edge is in scope.
        0
    } else {
        gs.latest_block_time().saturating_sub(WINDOWS[window_idx])
    };

    // First pass: collect edges that are live AND >= cutoff. Each
    // entry pairs the slot index with its generation so the wire
    // event carries the full handle.
    let mut visible_nodes: FxHashSet<NodeIdx> = FxHashSet::default();
    let mut visible_edges: Vec<(u32, u32, &super::GraphEdge)> = Vec::new();
    for (idx, slot) in gs.edges.iter().enumerate() {
        let Some(e) = slot.edge.as_ref() else { continue };
        if e.block_time < cutoff {
            continue;
        }
        visible_nodes.insert(e.src);
        visible_nodes.insert(e.dst);
        visible_edges.push((idx as u32, slot.generation, e));
    }

    let mut events: Vec<GraphDelta> =
        Vec::with_capacity(visible_nodes.len() + visible_edges.len() + 1);

    for &n in &visible_nodes {
        if let Some(pubkey) = gs.interner.lookup(n) {
            events.push(GraphDelta::NodeAdded {
                seq: 0,
                idx: n,
                pubkey: pubkey.to_string(),
            });
        }
    }

    for (eidx, generation, e) in &visible_edges {
        let mint = e
            .mint
            .map(|midx| gs.mint_interner.lookup(midx).unwrap_or("").to_string());
        events.push(GraphDelta::EdgeAdded {
            seq: 0,
            idx: *eidx,
            generation: *generation,
            src: e.src,
            dst: e.dst,
            mint,
            amount: e.amount,
            slot: e.slot,
            kind: e.kind.clone(),
        });
    }

    events
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::Edge;
    use crate::graph::GraphState;
    use crate::graph::window::MAX_WINDOW_IDX;

    fn make_edge(from: &str, to: &str, slot: u64) -> Edge {
        Edge {
            signature: format!("sig_{from}_{to}_{slot}"),
            instruction_idx: 0,
            slot,
            block_time: slot as u32,
            from_wallet: from.to_string(),
            to_wallet: to.to_string(),
            amount: 1_000_000,
            mint: String::new(),
            kind: String::new(),
            version: 1,
        }
    }

    fn make_edge_bt(from: &str, to: &str, slot: u64, block_time: u64) -> Edge {
        Edge {
            signature: format!("sig_{from}_{to}_{slot}"),
            instruction_idx: 0,
            slot,
            block_time: block_time as u32,
            from_wallet: from.to_string(),
            to_wallet: to.to_string(),
            amount: 1_000_000,
            mint: String::new(),
            kind: String::new(),
            version: 1,
        }
    }

    #[test]
    fn bootstrap_reproduces_graph_state_at_global_window() {
        let mut gs = GraphState::default();
        gs.ingest(&make_edge("AAA", "BBB", 100));
        gs.ingest(&make_edge("CCC", "DDD", 101));
        gs.ingest(&make_edge("BBB", "CCC", 102));

        let events = bootstrap_events(&gs, MAX_WINDOW_IDX);
        let mut nodes = 0;
        let mut edges = 0;
        for ev in &events {
            match ev {
                GraphDelta::NodeAdded { .. } => nodes += 1,
                GraphDelta::EdgeAdded { .. } => edges += 1,
                _ => {}
            }
        }
        assert_eq!(nodes, 4);
        assert_eq!(edges, 3);
        // Connectivity is not part of the bootstrap stream; verified
        // separately on `gs` directly.
        assert_eq!(gs.total_components(), 1);
    }

    #[test]
    fn bootstrap_filters_by_smaller_window() {
        let mut gs = GraphState::default();
        gs.ingest(&make_edge_bt("AAA", "BBB", 1, 1000));
        gs.ingest(&make_edge_bt("CCC", "DDD", 2, 2000));
        gs.ingest(&make_edge_bt("EEE", "FFF", 3, 4990));

        // Window 0 = 60s. Cutoff = 4990 - 60 = 4930. Only edge 3 visible.
        let events = bootstrap_events(&gs, 0);
        let edges: Vec<_> = events
            .iter()
            .filter_map(|e| match e {
                GraphDelta::EdgeAdded { idx, .. } => Some(*idx),
                _ => None,
            })
            .collect();
        assert_eq!(edges.len(), 1, "only the most recent edge fits in 60s window");
    }

    #[test]
    fn bootstrap_empty_state_produces_no_events() {
        let gs = GraphState::default();
        let events = bootstrap_events(&gs, MAX_WINDOW_IDX);
        assert!(events.is_empty());
    }
}
