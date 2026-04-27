/// Cold-start bootstrap: serialize current GraphState as a stream of
/// synthetic GraphDelta values. All events get seq=0 (no live seq).
///
/// Order per plan:
/// 1. NodeAdded for each live (non-tombstoned) node
/// 2. EdgeAdded for each non-tombstoned edge
/// 3. ComponentAssigned for each live node
use super::delta::{EdgeKind, GraphDelta};
use super::GraphState;

/// Produce all bootstrap events for the current state. Caller emits each
/// event WITHOUT an SSE `id:` field. After the last bootstrap event, the
/// caller emits `CaughtUp { seq: live_seq_at_release }` WITH an `id:` field,
/// then releases the read lock.
pub fn bootstrap_events(gs: &GraphState) -> Vec<GraphDelta> {
    let capacity = gs.interner.len() as usize * 3 + gs.live_edge_count() as usize;
    let mut events = Vec::with_capacity(capacity);

    // 1. NodeAdded for each live node.
    let node_capacity = gs.interner.capacity();
    for idx in 0..node_capacity {
        if let Some(pubkey) = gs.interner.lookup(idx) {
            events.push(GraphDelta::NodeAdded {
                seq: 0,
                idx,
                pubkey: pubkey.to_string(),
            });
        }
    }

    // 2. EdgeAdded for each live edge.
    for (idx, slot) in gs.edges.iter().enumerate() {
        let Some(e) = slot else { continue };
        let mint = e
            .mint
            .map(|midx| gs.mint_interner.lookup(midx).unwrap_or("").to_string());
        events.push(GraphDelta::EdgeAdded {
            seq: 0,
            idx: idx as u32,
            src: e.src,
            dst: e.dst,
            mint,
            amount: e.amount,
            slot: e.slot,
            kind: e.kind.clone(),
        });
    }

    // 3. ComponentAssigned for each live node.
    let node_capacity = gs.interner.capacity();
    for idx in 0..node_capacity {
        if gs.interner.lookup(idx).is_some() {
            let cid = gs.node_to_component.get(idx as usize).copied().unwrap_or(0);
            if cid != u64::MAX {
                events.push(GraphDelta::ComponentAssigned {
                    seq: 0,
                    node: idx,
                    component_id: cid,
                });
            }
        }
    }

    // 4. PositionsBatch for every live node so backend-UF clients can
    //    render at backend coords without first waiting for a layout tick.
    let positions = gs.all_positions();
    if !positions.is_empty() {
        events.push(GraphDelta::PositionsBatch { seq: 0, positions });
    }

    events
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::Edge;
    use crate::graph::GraphState;

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

    /// Apply a bootstrap event stream to an empty GraphState-equivalent
    /// tracking structure. Returns (nodes, edges, component_assigns).
    fn apply_bootstrap(events: &[GraphDelta]) -> (Vec<(u32, String)>, Vec<u32>, Vec<(u32, u64)>) {
        let mut nodes: Vec<(u32, String)> = Vec::new();
        let mut edges: Vec<u32> = Vec::new();
        let mut component_assigns: Vec<(u32, u64)> = Vec::new();
        for ev in events {
            match ev {
                GraphDelta::NodeAdded { idx, pubkey, .. } => nodes.push((*idx, pubkey.clone())),
                GraphDelta::EdgeAdded { idx, .. } => edges.push(*idx),
                GraphDelta::ComponentAssigned { node, component_id, .. } => {
                    component_assigns.push((*node, *component_id))
                }
                _ => {}
            }
        }
        (nodes, edges, component_assigns)
    }

    #[test]
    fn bootstrap_reproduces_graph_state() {
        let mut gs = GraphState::default();
        gs.ingest(&make_edge("AAA", "BBB", 100));
        gs.ingest(&make_edge("CCC", "DDD", 101));
        gs.ingest(&make_edge("BBB", "CCC", 102));

        let events = bootstrap_events(&gs);
        let (nodes, edges, assigns) = apply_bootstrap(&events);

        // 4 live nodes
        assert_eq!(nodes.len(), 4);
        // 3 edges
        assert_eq!(edges.len(), 3);
        // 4 component assignments (one per live node)
        assert_eq!(assigns.len(), 4);

        // All events have seq=0
        assert!(events
            .iter()
            .all(|e| matches!(e, GraphDelta::NodeAdded { seq: 0, .. }
                | GraphDelta::EdgeAdded { seq: 0, .. }
                | GraphDelta::ComponentAssigned { seq: 0, .. }
                | GraphDelta::PositionsBatch { seq: 0, .. })));
    }

    #[test]
    fn bootstrap_empty_state_produces_no_events() {
        let gs = GraphState::default();
        let events = bootstrap_events(&gs);
        assert!(events.is_empty());
    }
}
