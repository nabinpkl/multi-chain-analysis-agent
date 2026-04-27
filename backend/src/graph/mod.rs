pub mod bootstrap;
pub mod consumer;
pub mod delta;
pub mod expiry;
pub mod initial_position;
pub mod interner;
pub mod layout;
pub mod union_find;
pub mod window;

use rustc_hash::FxHashSet;

use crate::domain::Edge;
use delta::{EdgeKind, GraphDelta};
use expiry::EdgesByTime;
use interner::{NodeIdx, NodeInterner};
use union_find::{ComponentId, UnionFind};
use window::{NUM_WINDOWS, MAX_WINDOW_IDX, WINDOWS};

pub use delta::PositionUpdate;

/// Result of a single ingest call. `common` events fan out to every
/// per-window broadcast channel; `per_window[w]` events fan out only to
/// channel `w` (window-specific edge/node expiry).
#[derive(Default)]
pub struct IngestDeltas {
    pub common: Vec<GraphDelta>,
    pub per_window: [Vec<GraphDelta>; NUM_WINDOWS],
}

impl IngestDeltas {
    pub fn is_empty(&self) -> bool {
        self.common.is_empty() && self.per_window.iter().all(|v| v.is_empty())
    }

    /// Iterate every delta produced by an ingest, regardless of channel.
    /// Order: common first, then per-window in window-index order. Useful
    /// for tests; production code dispatches per channel.
    pub fn iter_all(&self) -> impl Iterator<Item = &GraphDelta> {
        self.common
            .iter()
            .chain(self.per_window.iter().flat_map(|v| v.iter()))
    }
}

/// Per-window state. The `MAX_WINDOW_IDX` slot is the source of truth for
/// global expiry  whatever falls off its front is also tombstoned in the
/// global slab. Smaller windows merely emit deltas; the underlying edge
/// stays live in the slab until the largest window expires it.
pub(super) struct WindowState {
    /// Edge indices ordered by `block_time`. Front = oldest.
    pub(super) edges_by_time: EdgesByTime,
    /// Per-node count of live edges incident to this node in this window.
    /// Used to drive per-window NodeAdded / NodeExpired transitions.
    pub(super) edge_count_per_node: Vec<u32>,
}

impl WindowState {
    pub(super) fn new() -> Self {
        Self {
            edges_by_time: EdgesByTime::new(),
            edge_count_per_node: Vec::new(),
        }
    }
}

/// Mirrors `nodeSize()` in `frontend/src/hooks/use-raw-stream.ts`.
/// degree<=1 -> 0.8; else 1.5 + sqrt(min(1,(d-1)/59)) * 8.5.
pub(super) fn node_size_for_degree(degree: u32) -> f32 {
    const MIN_PX: f32 = 1.5;
    const MAX_PX: f32 = 10.0;
    const REF_DEGREE: f32 = 60.0;
    if degree <= 1 {
        return 0.8;
    }
    let norm = (((degree as f32) - 1.0) / (REF_DEGREE - 1.0)).min(1.0);
    MIN_PX + norm.sqrt() * (MAX_PX - MIN_PX)
}

pub type EdgeIdx = u32;
type MintIdx = u32;

pub struct GraphEdge {
    pub src: NodeIdx,
    pub dst: NodeIdx,
    pub amount: u64,
    pub mint: Option<MintIdx>,
    pub slot: u64,
    pub block_time: u64,
    pub kind: Option<EdgeKind>,
}

pub struct GraphState {
    pub(super) interner: NodeInterner,
    pub(super) mint_interner: NodeInterner,

    pub edges: Vec<Option<GraphEdge>>,
    pub(super) free_edge_slots: Vec<EdgeIdx>,

    pub(super) out_adj: Vec<Vec<EdgeIdx>>,
    pub(super) in_adj: Vec<Vec<EdgeIdx>>,

    pub(super) uf: UnionFind,

    /// Dense per-node component membership. Indexed by NodeIdx.
    /// `u64::MAX` means the node slot is dead (freed by expiry).
    pub(super) node_to_component: Vec<ComponentId>,

    /// Monotonic component id counter. Never reused.
    component_id_seq: ComponentId,

    /// Monotonic delta sequence counter.
    seq_counter: u64,

    /// Latest block_time seen so far. Per-window cutoff = latest - WINDOWS[w].
    latest_block_time: u64,

    /// Five overlapping views over the same global state, ordered by
    /// `WINDOWS` (60s, 300s, 900s, 1800s, 3600s). Index 4 (3600s) is the
    /// global retention boundary  edges falling off its front are
    /// tombstoned from the slab.
    pub(super) windows: [WindowState; NUM_WINDOWS],

    last_ingested_slot: Option<u64>,

    /// Per-node position/velocity slabs, indexed by NodeIdx.
    /// Lockstep with interner; freed slots reset to zero on reuse.
    pub(super) pos_x: Vec<f32>,
    pub(super) pos_y: Vec<f32>,
    pub(super) vel_x: Vec<f32>,
    pub(super) vel_y: Vec<f32>,

    /// Unique-neighbor degree per node. Incremented when the FIRST
    /// edge between a pair is added; decremented when the LAST edge
    /// between a pair is tombstoned. Mirrors frontend `degree` attr
    /// semantics so megahub detection + size derivation agree.
    pub(super) unique_degree: Vec<u32>,
    /// Render size derived from `unique_degree`, mirroring the JS
    /// `nodeSize()` function in `use-raw-stream.ts`.
    pub(super) size: Vec<f32>,
}

impl Default for GraphState {
    fn default() -> Self {
        Self {
            interner: NodeInterner::default(),
            mint_interner: NodeInterner::default(),
            edges: Vec::new(),
            free_edge_slots: Vec::new(),
            out_adj: Vec::new(),
            in_adj: Vec::new(),
            uf: UnionFind::default(),
            node_to_component: Vec::new(),
            component_id_seq: 0,
            seq_counter: 0,
            latest_block_time: 0,
            windows: std::array::from_fn(|_| WindowState::new()),
            last_ingested_slot: None,
            pos_x: Vec::new(),
            pos_y: Vec::new(),
            vel_x: Vec::new(),
            vel_y: Vec::new(),
            unique_degree: Vec::new(),
            size: Vec::new(),
        }
    }
}

impl GraphState {
    /// Allocate a fresh monotonic ComponentId.
    pub(super) fn alloc_component_id(&mut self) -> ComponentId {
        let id = self.component_id_seq;
        self.component_id_seq += 1;
        id
    }

    /// Increment and return the next seq number.
    pub(super) fn next_seq(&mut self) -> u64 {
        let s = self.seq_counter;
        self.seq_counter += 1;
        s
    }

    /// Public seq allocator for tasks outside the graph module
    /// (e.g. the layout-tick loop in main.rs).
    pub fn alloc_seq(&mut self) -> u64 {
        self.next_seq()
    }

    /// Current seq counter (for CaughtUp snapshot during bootstrap).
    pub fn current_seq(&self) -> u64 {
        self.seq_counter
    }

    /// Count live (non-tombstoned) edges.
    pub fn live_edge_count(&self) -> u32 {
        self.edges.iter().filter(|s| s.is_some()).count() as u32
    }

    /// Intern a node if new. Grows adjacency lists and UF as needed.
    fn intern_node(&mut self, pubkey: &str, slot: u64) -> (NodeIdx, bool) {
        let (idx, newly_inserted) = self.interner.intern(pubkey);
        if newly_inserted {
            // Grow parallel structures.
            while self.out_adj.len() <= idx as usize {
                self.out_adj.push(Vec::new());
            }
            while self.in_adj.len() <= idx as usize {
                self.in_adj.push(Vec::new());
            }
            while self.node_to_component.len() <= idx as usize {
                self.node_to_component.push(u64::MAX);
            }
            while self.pos_x.len() <= idx as usize {
                self.pos_x.push(0.0);
                self.pos_y.push(0.0);
                self.vel_x.push(0.0);
                self.vel_y.push(0.0);
                self.unique_degree.push(0);
                self.size.push(node_size_for_degree(0));
            }
            for w in 0..NUM_WINDOWS {
                while self.windows[w].edge_count_per_node.len() <= idx as usize {
                    self.windows[w].edge_count_per_node.push(0);
                }
                self.windows[w].edge_count_per_node[idx as usize] = 0;
            }
            // Reset pos/vel/degree/size on reuse (free-list path).
            self.pos_x[idx as usize] = 0.0;
            self.pos_y[idx as usize] = 0.0;
            self.vel_x[idx as usize] = 0.0;
            self.vel_y[idx as usize] = 0.0;
            self.unique_degree[idx as usize] = 0;
            self.size[idx as usize] = node_size_for_degree(0);

            // Allocate a fresh component for this singleton.
            let cid = self.alloc_component_id();
            self.node_to_component[idx as usize] = cid;
            self.uf.push_singleton(cid);
        }
        let _ = slot; // slot recorded on edge; not on node in this version
        (idx, newly_inserted)
    }

    /// Core ingest: advances cutoff, drains expired edges per window,
    /// adds new edge, settles splits. Returns deltas split into:
    ///   - `common`: events that fan out to every window's channel
    ///     (NodeAdded, EdgeAdded, ComponentAssigned).
    ///   - `per_window[w]`: events scoped to a single window's channel
    ///     (window-boundary EdgeExpired and NodeExpired).
    pub fn ingest(&mut self, edge: &Edge) -> IngestDeltas {
        let mut out = IngestDeltas::default();

        // 1. Advance block_time cutoff.
        let bt = edge.block_time as u64;
        self.latest_block_time = self.latest_block_time.max(bt);

        // 2. Drain expired edges per window. The largest window (index
        //    MAX_WINDOW_IDX) also tombstones from the global slab.
        let mut dirty_components: FxHashSet<ComponentId> = FxHashSet::default();
        for w in 0..NUM_WINDOWS {
            let cutoff = self.latest_block_time.saturating_sub(WINDOWS[w]);
            let is_global = w == MAX_WINDOW_IDX;
            loop {
                let Some(front_idx) = self.windows[w].edges_by_time.front() else {
                    break;
                };
                let front_bt = match &self.edges[front_idx as usize] {
                    Some(e) => e.block_time,
                    None => {
                        // Tombstoned entry still in this window's deque  drop.
                        self.windows[w].edges_by_time.pop_front();
                        continue;
                    }
                };
                if front_bt >= cutoff {
                    break;
                }
                self.windows[w].edges_by_time.pop_front();

                if is_global {
                    // Track dirty component before tombstoning so split
                    // detection runs after global expiry.
                    let cid = {
                        let e = self.edges[front_idx as usize].as_ref().unwrap();
                        self.node_to_component
                            .get(e.src as usize)
                            .copied()
                            .unwrap_or(u64::MAX)
                    };
                    if cid != u64::MAX {
                        dirty_components.insert(cid);
                    }
                    // Drive global tombstoning + emit window-local expiry.
                    let expired = self.tombstone_edge_for_window(front_idx, w, true);
                    out.per_window[w].extend(expired);
                } else {
                    // Smaller-than-global window: edge stays alive in slab,
                    // we only emit the window-scoped expiry deltas.
                    let expired = self.tombstone_edge_for_window(front_idx, w, false);
                    out.per_window[w].extend(expired);
                }
            }
        }

        // 3. Add new edge. Common deltas (NodeAdded, EdgeAdded,
        //    ComponentAssigned) fan out to all channels.
        self.add_edge(edge, &mut out);

        // 4. Settle splits for dirty components via rayon BFS. These
        //    ComponentAssigned events are global so they go on `common`.
        let settle_deltas = self.settle_components(dirty_components);
        out.common.extend(settle_deltas);

        self.last_ingested_slot = Some(edge.slot);
        out
    }

    /// Add a single edge (sub-routine of ingest). Handles node interning,
    /// edge slab allocation, adjacency update, UF union, and component
    /// assignment events. All produced deltas are common (every window
    /// channel sees them) since a freshly-arriving edge sits at the tip
    /// of every window.
    fn add_edge(&mut self, edge: &Edge, out: &mut IngestDeltas) {
        let bt = edge.block_time as u64;

        let (src_idx, src_new) = self.intern_node(&edge.from_wallet, edge.slot);
        if src_new {
            let dst_known = self.interner.lookup_idx(&edge.to_wallet);
            let (x, y) = initial_position::compute(self, &edge.from_wallet, dst_known);
            self.pos_x[src_idx as usize] = x;
            self.pos_y[src_idx as usize] = y;

            let seq = self.next_seq();
            out.common.push(GraphDelta::NodeAdded {
                seq,
                idx: src_idx,
                pubkey: edge.from_wallet.clone(),
            });
            let cid = self.node_to_component[src_idx as usize];
            let seq2 = self.next_seq();
            out.common.push(GraphDelta::ComponentAssigned {
                seq: seq2,
                node: src_idx,
                component_id: cid,
            });
        }

        let (dst_idx, dst_new) = self.intern_node(&edge.to_wallet, edge.slot);
        if dst_new {
            let (x, y) = initial_position::compute(self, &edge.to_wallet, Some(src_idx));
            self.pos_x[dst_idx as usize] = x;
            self.pos_y[dst_idx as usize] = y;

            let seq = self.next_seq();
            out.common.push(GraphDelta::NodeAdded {
                seq,
                idx: dst_idx,
                pubkey: edge.to_wallet.clone(),
            });
            let cid = self.node_to_component[dst_idx as usize];
            let seq2 = self.next_seq();
            out.common.push(GraphDelta::ComponentAssigned {
                seq: seq2,
                node: dst_idx,
                component_id: cid,
            });
        }

        let mint_idx = if edge.mint.is_empty() {
            None
        } else {
            Some(self.mint_interner.intern(&edge.mint).0)
        };

        let kind = match edge.kind.as_str() {
            "mint" => Some(EdgeKind::Mint),
            "burn" => Some(EdgeKind::Burn),
            _ => None,
        };

        let graph_edge = GraphEdge {
            src: src_idx,
            dst: dst_idx,
            amount: edge.amount,
            mint: mint_idx,
            slot: edge.slot,
            block_time: bt,
            kind,
        };

        let pair_already_connected = self.has_edge_between(src_idx, dst_idx);
        let edge_idx = self.alloc_edge_slot(graph_edge);

        // Insert into every window whose cutoff this edge satisfies.
        // For monotone arrivals this is all 5; for slightly-late edges
        // a smaller window may already be past cutoff and skip.
        for w in 0..NUM_WINDOWS {
            let cutoff = self.latest_block_time.saturating_sub(WINDOWS[w]);
            if bt < cutoff {
                continue;
            }
            self.windows[w].edges_by_time.insert(edge_idx, bt, &self.edges);
            // Bump per-window edge count for both endpoints.
            self.windows[w].edge_count_per_node[src_idx as usize] =
                self.windows[w].edge_count_per_node[src_idx as usize].saturating_add(1);
            if dst_idx != src_idx {
                self.windows[w].edge_count_per_node[dst_idx as usize] =
                    self.windows[w].edge_count_per_node[dst_idx as usize].saturating_add(1);
            }
        }

        self.out_adj[src_idx as usize].push(edge_idx);
        self.in_adj[dst_idx as usize].push(edge_idx);

        if !pair_already_connected && src_idx != dst_idx {
            self.unique_degree[src_idx as usize] =
                self.unique_degree[src_idx as usize].saturating_add(1);
            self.unique_degree[dst_idx as usize] =
                self.unique_degree[dst_idx as usize].saturating_add(1);
            self.size[src_idx as usize] =
                node_size_for_degree(self.unique_degree[src_idx as usize]);
            self.size[dst_idx as usize] =
                node_size_for_degree(self.unique_degree[dst_idx as usize]);
        }

        let e_ref = self.edges[edge_idx as usize].as_ref().unwrap();
        let mint_str = e_ref
            .mint
            .map(|midx| self.mint_interner.lookup(midx).unwrap_or("").to_string());
        let seq = self.next_seq();
        out.common.push(GraphDelta::EdgeAdded {
            seq,
            idx: edge_idx,
            src: src_idx,
            dst: dst_idx,
            mint: mint_str,
            amount: edge.amount,
            slot: edge.slot,
            kind: self.edges[edge_idx as usize]
                .as_ref()
                .unwrap()
                .kind
                .clone(),
        });

        // Union-Find merge.
        let ra = self.uf.find(src_idx);
        let rb = self.uf.find(dst_idx);
        if ra != rb {
            let size_a = self.uf.size_of_root(ra);
            let size_b = self.uf.size_of_root(rb);
            let (smaller_root, larger_cid) = if size_a <= size_b {
                let larger_cid = self.uf.component_id_of_root(rb);
                (ra, larger_cid)
            } else {
                let larger_cid = self.uf.component_id_of_root(ra);
                (rb, larger_cid)
            };
            let smaller_cid = self.uf.component_id_of_root(smaller_root);

            self.uf.union(src_idx, dst_idx);

            // O(N) scan over node_to_component.
            for i in 0..self.node_to_component.len() {
                if self.node_to_component[i] == smaller_cid {
                    self.node_to_component[i] = larger_cid;
                    let seq = self.next_seq();
                    out.common.push(GraphDelta::ComponentAssigned {
                        seq,
                        node: i as NodeIdx,
                        component_id: larger_cid,
                    });
                }
            }
        }
    }

    pub fn total_nodes(&self) -> u32 {
        self.interner.len()
    }

    pub fn total_edges(&self) -> u32 {
        self.live_edge_count()
    }

    pub fn total_components(&self) -> u32 {
        // Derive on demand: count distinct non-dead component ids that are
        // also UF roots.  A quicker approximation: count UF roots among live
        // nodes.
        let mut seen = FxHashSet::default();
        for (i, &cid) in self.node_to_component.iter().enumerate() {
            if cid == u64::MAX {
                continue;
            }
            // Only count a component once (via the UF root).
            let root = {
                let mut r = i as NodeIdx;
                // Find without path compression (we don't have &mut self here).
                // Use find_immut.
                r = self.uf.find_immut(r);
                r
            };
            seen.insert(root);
        }
        seen.len() as u32
    }

    pub fn largest_component_size(&self) -> u32 {
        // Count nodes per component_id.
        let mut counts: rustc_hash::FxHashMap<ComponentId, u32> =
            rustc_hash::FxHashMap::default();
        for &cid in &self.node_to_component {
            if cid == u64::MAX {
                continue;
            }
            *counts.entry(cid).or_insert(0) += 1;
        }
        counts.values().copied().max().unwrap_or(0)
    }

    pub fn last_ingested_slot(&self) -> Option<u64> {
        self.last_ingested_slot
    }

    /// Tip of the ingest stream, in `block_time` (seconds since epoch).
    /// Per-window cutoff = `latest_block_time() - WINDOWS[w]`.
    pub fn latest_block_time(&self) -> u64 {
        self.latest_block_time
    }

    /// Is there at least one live edge in either direction between
    /// `a` and `b`? Used to detect first-edge-for-this-pair so we can
    /// bump unique_degree only when the pair becomes connected.
    pub(super) fn has_edge_between(&self, a: NodeIdx, b: NodeIdx) -> bool {
        if (a as usize) >= self.out_adj.len() || (b as usize) >= self.in_adj.len() {
            return false;
        }
        for &eidx in &self.out_adj[a as usize] {
            if let Some(e) = &self.edges[eidx as usize] {
                if e.dst == b {
                    return true;
                }
            }
        }
        for &eidx in &self.in_adj[a as usize] {
            if let Some(e) = &self.edges[eidx as usize] {
                if e.src == b {
                    return true;
                }
            }
        }
        false
    }

    /// Snapshot every live node's (idx, x, y) for cold-start broadcast.
    pub fn all_positions(&self) -> Vec<PositionUpdate> {
        let cap = self.interner.capacity();
        let mut out = Vec::with_capacity(self.interner.len() as usize);
        for idx in 0..cap {
            if self.interner.lookup(idx).is_some() {
                out.push(PositionUpdate {
                    idx,
                    x: self.pos_x[idx as usize],
                    y: self.pos_y[idx as usize],
                });
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::Edge;

    fn make_edge(from: &str, to: &str, slot: u64, block_time: u64) -> Edge {
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
    fn ingest_two_edges_nodes_and_components() {
        let mut gs = GraphState::default();

        let e1 = make_edge("AAA", "BBB", 100, 1000);
        let deltas1 = gs.ingest(&e1);

        assert!(deltas1
            .iter_all()
            .any(|d| matches!(d, GraphDelta::NodeAdded { pubkey, .. } if pubkey == "AAA")));
        assert!(deltas1
            .iter_all()
            .any(|d| matches!(d, GraphDelta::NodeAdded { pubkey, .. } if pubkey == "BBB")));
        assert!(deltas1
            .iter_all()
            .any(|d| matches!(d, GraphDelta::EdgeAdded { .. })));
        assert!(deltas1
            .iter_all()
            .any(|d| matches!(d, GraphDelta::ComponentAssigned { .. })));

        assert_eq!(gs.total_nodes(), 2);
        assert_eq!(gs.total_edges(), 1);
        assert_eq!(gs.total_components(), 1);

        let e2 = make_edge("CCC", "DDD", 101, 1001);
        let _deltas2 = gs.ingest(&e2);

        assert_eq!(gs.total_nodes(), 4);
        assert_eq!(gs.total_edges(), 2);
        assert_eq!(gs.total_components(), 2);

        let e3 = make_edge("BBB", "CCC", 102, 1002);
        let _deltas3 = gs.ingest(&e3);

        assert_eq!(gs.total_nodes(), 4);
        assert_eq!(gs.total_edges(), 3);
        assert_eq!(gs.total_components(), 1);
        assert_eq!(gs.last_ingested_slot(), Some(102));
    }

    #[test]
    fn ingest_with_edges_crossing_cutoff() {
        let mut gs = GraphState::default();

        // Edge at block_time=1000
        let e1 = make_edge("AAA", "BBB", 1, 1000);
        gs.ingest(&e1);

        // Edge at block_time=5000  advances 3600s cutoff to 1400
        // so e1 (block_time=1000) expires from every window.
        let e2 = make_edge("CCC", "DDD", 2, 5000);
        let deltas = gs.ingest(&e2);
        let deltas: Vec<_> = deltas.iter_all().cloned().collect();

        let edge_expired = deltas
            .iter()
            .filter(|d| matches!(d, GraphDelta::EdgeExpired { .. }))
            .count();
        let node_expired = deltas
            .iter()
            .filter(|d| matches!(d, GraphDelta::NodeExpired { .. }))
            .count();

        // e1 lies below every window's cutoff at bt=5000, so each of the
        // 5 windows emits its own EdgeExpired and pair of NodeExpired.
        assert_eq!(edge_expired, NUM_WINDOWS, "one EdgeExpired per window");
        assert_eq!(node_expired, NUM_WINDOWS * 2, "AAA+BBB orphan in each window");
    }
}
