pub mod consumer;
pub mod delta;
pub mod interner;
pub mod union_find;

use rustc_hash::FxHashMap;

use crate::api::raw::EdgeKind;
use crate::domain::Edge;
use delta::GraphDelta;
use interner::{NodeIdx, NodeInterner};
use union_find::UnionFind;

type EdgeIdx = u32;
type MintIdx = u32;

struct GraphEdge {
    src: NodeIdx,
    dst: NodeIdx,
    amount: u64,
    mint: Option<MintIdx>,
    slot: u64,
    kind: Option<EdgeKind>,
}

struct ComponentMeta {
    size: u32,
    edge_count: u64,
    first_seen_slot: u64,
    last_seen_slot: u64,
}

#[derive(Default)]
pub struct GraphState {
    interner: NodeInterner,
    mint_interner: NodeInterner,

    edges: Vec<GraphEdge>,
    out_adj: Vec<Vec<EdgeIdx>>,
    in_adj: Vec<Vec<EdgeIdx>>,

    uf: UnionFind,
    components: FxHashMap<NodeIdx, ComponentMeta>,

    last_ingested_slot: Option<u64>,
}

impl GraphState {
    pub fn ingest(&mut self, edge: &Edge) -> Vec<GraphDelta> {
        let mut deltas = Vec::new();

        // Intern source and destination wallets
        let (src_idx, src_new) = self.interner.intern(&edge.from_wallet);
        if src_new {
            self.out_adj.push(Vec::new());
            self.in_adj.push(Vec::new());
            self.uf.push_singleton();
            self.components.insert(
                src_idx,
                ComponentMeta {
                    size: 1,
                    edge_count: 0,
                    first_seen_slot: edge.slot,
                    last_seen_slot: edge.slot,
                },
            );
            deltas.push(GraphDelta::NodeAdded {
                idx: src_idx,
                pubkey: edge.from_wallet.clone(),
            });
        }

        let (dst_idx, dst_new) = self.interner.intern(&edge.to_wallet);
        if dst_new {
            self.out_adj.push(Vec::new());
            self.in_adj.push(Vec::new());
            self.uf.push_singleton();
            self.components.insert(
                dst_idx,
                ComponentMeta {
                    size: 1,
                    edge_count: 0,
                    first_seen_slot: edge.slot,
                    last_seen_slot: edge.slot,
                },
            );
            deltas.push(GraphDelta::NodeAdded {
                idx: dst_idx,
                pubkey: edge.to_wallet.clone(),
            });
        }

        // Intern mint if present
        let mint_idx = if edge.mint.is_empty() {
            None
        } else {
            Some(self.mint_interner.intern(&edge.mint).0)
        };

        // Map kind string to EdgeKind enum
        let kind = match edge.kind.as_str() {
            "mint" => Some(EdgeKind::Mint),
            "burn" => Some(EdgeKind::Burn),
            _ => None,
        };

        // Store edge
        let edge_idx = self.edges.len() as EdgeIdx;
        self.edges.push(GraphEdge {
            src: src_idx,
            dst: dst_idx,
            amount: edge.amount,
            mint: mint_idx,
            slot: edge.slot,
            kind,
        });

        // Update adjacency lists
        self.out_adj[src_idx as usize].push(edge_idx);
        self.in_adj[dst_idx as usize].push(edge_idx);

        deltas.push(GraphDelta::EdgeAdded {
            idx: edge_idx,
            src: src_idx,
            dst: dst_idx,
        });

        // Union-Find: try to merge components
        match self.uf.union(src_idx, dst_idx) {
            Some(merge) => {
                // Merge the two component metadata entries
                let absorbed_meta = self.components.remove(&merge.absorbed_root).unwrap_or(ComponentMeta {
                    size: 1,
                    edge_count: 0,
                    first_seen_slot: edge.slot,
                    last_seen_slot: edge.slot,
                });
                let surviving_meta = self.components.entry(merge.surviving_root).or_insert(ComponentMeta {
                    size: 1,
                    edge_count: 0,
                    first_seen_slot: edge.slot,
                    last_seen_slot: edge.slot,
                });
                let new_size = absorbed_meta.size + surviving_meta.size;
                surviving_meta.size = new_size;
                surviving_meta.edge_count += absorbed_meta.edge_count + 1;
                surviving_meta.last_seen_slot = edge.slot.max(surviving_meta.last_seen_slot);
                if absorbed_meta.first_seen_slot < surviving_meta.first_seen_slot {
                    surviving_meta.first_seen_slot = absorbed_meta.first_seen_slot;
                }

                deltas.push(GraphDelta::ComponentMerged {
                    absorbed_root: merge.absorbed_root,
                    surviving_root: merge.surviving_root,
                    new_size,
                });
            }
            None => {
                // Same component already: just bump edge_count + last_seen on the root
                let root = self.uf.find(src_idx);
                if let Some(meta) = self.components.get_mut(&root) {
                    meta.edge_count += 1;
                    meta.last_seen_slot = meta.last_seen_slot.max(edge.slot);
                }
            }
        }

        self.last_ingested_slot = Some(edge.slot);

        deltas
    }

    pub fn total_nodes(&self) -> u32 {
        self.interner.len()
    }

    pub fn total_edges(&self) -> u32 {
        self.edges.len() as u32
    }

    pub fn total_components(&self) -> u32 {
        self.uf.count_roots()
    }

    pub fn largest_component_size(&self) -> u32 {
        self.components.values().map(|m| m.size).max().unwrap_or(0)
    }

    pub fn last_ingested_slot(&self) -> Option<u64> {
        self.last_ingested_slot
    }

    /// Iterate nodes by insertion order from `start` for up to `limit` entries.
    /// Returns (pubkey, component_root) pairs.
    pub fn iter_nodes_from(
        &mut self,
        start: u32,
        limit: u32,
    ) -> Vec<(String, u32)> {
        let total = self.interner.len();
        if start >= total {
            return Vec::new();
        }
        let end = (start + limit).min(total);
        (start..end)
            .map(|idx| {
                let pubkey = self.interner.lookup(idx).unwrap_or("").to_string();
                let component_id = self.uf.find(idx);
                (pubkey, component_id)
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::Edge;

    fn make_edge(from: &str, to: &str, slot: u64) -> Edge {
        Edge {
            signature: format!("sig_{from}_{to}"),
            instruction_idx: 0,
            slot,
            block_time: 0,
            from_wallet: from.to_string(),
            to_wallet: to.to_string(),
            amount: 1_000_000,
            mint: String::new(),
            kind: String::new(),
            version: 1,
        }
    }

    #[test]
    fn ingest_two_edges_nodes_and_components() {
        let mut gs = GraphState::default();

        // First edge: A -> B
        let e1 = make_edge("AAA", "BBB", 100);
        let deltas1 = gs.ingest(&e1);

        // Should emit NodeAdded(A), NodeAdded(B), EdgeAdded, ComponentMerged
        assert!(deltas1.iter().any(|d| matches!(d, GraphDelta::NodeAdded { pubkey, .. } if pubkey == "AAA")));
        assert!(deltas1.iter().any(|d| matches!(d, GraphDelta::NodeAdded { pubkey, .. } if pubkey == "BBB")));
        assert!(deltas1.iter().any(|d| matches!(d, GraphDelta::EdgeAdded { .. })));
        assert!(deltas1.iter().any(|d| matches!(d, GraphDelta::ComponentMerged { new_size: 2, .. })));

        assert_eq!(gs.total_nodes(), 2);
        assert_eq!(gs.total_edges(), 1);
        assert_eq!(gs.total_components(), 1);

        // Second edge: C -> D (separate component)
        let e2 = make_edge("CCC", "DDD", 101);
        let deltas2 = gs.ingest(&e2);

        assert_eq!(gs.total_nodes(), 4);
        assert_eq!(gs.total_edges(), 2);
        assert_eq!(gs.total_components(), 2);
        assert!(deltas2.iter().any(|d| matches!(d, GraphDelta::ComponentMerged { new_size: 2, .. })));

        // Third edge: B -> C (merges two components)
        let e3 = make_edge("BBB", "CCC", 102);
        let deltas3 = gs.ingest(&e3);

        assert_eq!(gs.total_nodes(), 4);
        assert_eq!(gs.total_edges(), 3);
        assert_eq!(gs.total_components(), 1);
        assert!(deltas3.iter().any(|d| matches!(d, GraphDelta::ComponentMerged { new_size: 4, .. })));
        assert_eq!(gs.last_ingested_slot(), Some(102));
    }

    #[test]
    fn ingest_duplicate_edge_same_component() {
        let mut gs = GraphState::default();
        let e = make_edge("AAA", "BBB", 100);
        gs.ingest(&e);
        // Ingest same pair again - no new nodes, no merge (already same component)
        let deltas = gs.ingest(&e);
        assert_eq!(gs.total_nodes(), 2);
        assert!(!deltas.iter().any(|d| matches!(d, GraphDelta::NodeAdded { .. })));
        assert!(!deltas.iter().any(|d| matches!(d, GraphDelta::ComponentMerged { .. })));
        assert_eq!(gs.total_edges(), 2); // edge count increments
    }
}
