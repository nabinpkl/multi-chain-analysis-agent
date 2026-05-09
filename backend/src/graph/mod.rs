pub mod bootstrap;
pub mod consumer;
pub mod delta;
pub mod expiry;
pub mod interner;
pub mod union_find;
pub mod window;

use rustc_hash::FxHashSet;

use crate::domain::Edge;
use delta::{EdgeKind, GraphDelta};
use expiry::EdgesByTime;
use interner::{NodeIdx, NodeInterner};
use union_find::{ComponentId, UnionFind};
use window::{NUM_WINDOWS, MAX_WINDOW_IDX, WINDOWS};

/// Result of a single ingest call. `per_window[w]` events fan out
/// only to channel `w` and cover everything window-scoped: the
/// per-window `NodeAdded` on 0->1 edge-count transition, the
/// matching `EdgeAdded`, plus expiry deltas.
///
/// `common` exists as scaffolding for future cross-window events
/// (e.g. analytics broadcasts). It is currently always empty.
/// Component IDs are still tracked internally on `GraphState` for
/// stats endpoints but no longer streamed; the frontend computes
/// window-correct connectivity itself from the edge stream.
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

pub type EdgeIdx = u32;
type MintIdx = u32;

/// External edge handle. Pairs a slab slot index with the slot's
/// generation tag so that handles to the same slot at different
/// generations are distinct.
///
/// Lookup goes through the slab via `GraphState::get_edge(EdgeId)`,
/// which validates the generation. A handle whose generation no
/// longer matches the slot's current generation is stale and lookup
/// returns `None`. This makes slot reuse safe under unbounded
/// out-of-order delivery: a stale `EdgeAdded` for a recycled slot
/// can never collide with the live edge that now occupies it.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash)]
pub struct EdgeId {
    pub idx: EdgeIdx,
    /// Slot generation tag. Field name avoids the `gen` reserved
    /// keyword in Rust 2024; the wire format still uses "gen".
    pub generation: u32,
}

pub struct GraphEdge {
    pub src: NodeIdx,
    pub dst: NodeIdx,
    pub amount: u64,
    pub mint: Option<MintIdx>,
    pub slot: u64,
    pub block_time: u64,
    pub kind: Option<EdgeKind>,
}

/// One slab entry. `generation` increments every time `edge` is
/// reassigned (i.e. once per allocation that reuses the slot). Bare
/// `Option<GraphEdge>` is not enough because adjacency lists, the
/// time-ordered deque, and downstream consumers all carry handles
/// that must distinguish "the edge that was here yesterday" from
/// "the edge that is here now," even when the slot index is the
/// same.
pub struct EdgeSlot {
    pub generation: u32,
    pub edge: Option<GraphEdge>,
}

pub struct GraphState {
    pub(super) interner: NodeInterner,
    pub(super) mint_interner: NodeInterner,

    pub edges: Vec<EdgeSlot>,
    pub(super) free_edge_slots: Vec<EdgeIdx>,

    pub(super) out_adj: Vec<Vec<EdgeId>>,
    pub(super) in_adj: Vec<Vec<EdgeId>>,

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

    /// Six overlapping views over the same global state, ordered by
    /// `WINDOWS` (10s, 60s, 300s, 900s, 1800s, 3600s). Index
    /// `MAX_WINDOW_IDX` (3600s) is the global retention boundary; edges
    /// falling off its front are tombstoned from the slab.
    pub(super) windows: [WindowState; NUM_WINDOWS],

    last_ingested_slot: Option<u64>,
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

    /// Current seq counter (for CaughtUp snapshot during bootstrap).
    pub fn current_seq(&self) -> u64 {
        self.seq_counter
    }

    /// Count live (non-tombstoned) edges.
    pub fn live_edge_count(&self) -> u32 {
        self.edges.iter().filter(|s| s.edge.is_some()).count() as u32
    }

    /// Validate `id` against the slab's current generation and return
    /// the live edge if it matches. Returns `None` for stale handles
    /// (slot reused under a newer generation), tombstoned slots, or
    /// out-of-bounds indices.
    pub(super) fn get_edge(&self, id: EdgeId) -> Option<&GraphEdge> {
        let slot = self.edges.get(id.idx as usize)?;
        if slot.generation != id.generation {
            return None;
        }
        slot.edge.as_ref()
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
            for w in 0..NUM_WINDOWS {
                while self.windows[w].edge_count_per_node.len() <= idx as usize {
                    self.windows[w].edge_count_per_node.push(0);
                }
                self.windows[w].edge_count_per_node[idx as usize] = 0;
            }

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
    ///   - `common`: scaffolding for cross-window broadcast events.
    ///     Currently always empty.
    ///   - `per_window[w]`: every window-scoped delta, including the
    ///     `NodeAdded` / `EdgeAdded` pair for edges visible in window
    ///     `w`, plus `EdgeExpired` / `NodeExpired` when they fall off
    ///     `w`.
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
                let Some(front_id) = self.windows[w].edges_by_time.front() else {
                    break;
                };
                let front_bt = match self.get_edge(front_id) {
                    Some(e) => e.block_time,
                    None => {
                        // Stale or tombstoned entry still in this
                        // window's deque (generation no longer
                        // matches): drop it.
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
                        let e = self.get_edge(front_id).unwrap();
                        self.node_to_component
                            .get(e.src as usize)
                            .copied()
                            .unwrap_or(u64::MAX)
                    };
                    if cid != u64::MAX {
                        dirty_components.insert(cid);
                    }
                    // Drive global tombstoning + emit window-local expiry.
                    let expired = self.tombstone_edge_for_window(front_id, w, true);
                    out.per_window[w].extend(expired);
                } else {
                    // Smaller-than-global window: edge stays alive in slab,
                    // we only emit the window-scoped expiry deltas.
                    let expired = self.tombstone_edge_for_window(front_id, w, false);
                    out.per_window[w].extend(expired);
                }
            }
        }

        // 3. Add new edge. Per-window NodeAdded/EdgeAdded fan to the
        //    matching window channels.
        self.add_edge(edge, &mut out);

        // 4. Settle splits for dirty components. Updates internal
        //    `node_to_component` so stats endpoints (and any future
        //    in-process consumer) see window-correct connectivity.
        //    No events emitted: connectivity is recomputed on the
        //    frontend from the edge stream.
        self.settle_components(dirty_components);

        self.last_ingested_slot = Some(edge.slot);
        out
    }

    /// Add a single edge (sub-routine of ingest). Handles node interning,
    /// edge slab allocation, adjacency update, and UF union.
    ///
    /// `NodeAdded` / `EdgeAdded` are emitted on `per_window[w]` for
    /// each window the edge satisfies (`bt >= cutoff_w`). A node's
    /// `NodeAdded` fires whenever its per-window edge count transitions
    /// from 0 to 1, so a wallet that was `NodeExpired` from the 10s
    /// view and then comes back is announced again on that window's
    /// channel. Without this, the frontend's `idxToPubkey` map (which
    /// it deletes on `NodeExpired`) would never rebind for the
    /// returning wallet and subsequent `EdgeAdded`s for it would be
    /// silently dropped.
    ///
    /// Component assignment is updated in `node_to_component` for
    /// internal correctness (stats endpoints, future analytics) but
    /// no longer emitted on the wire. The frontend computes
    /// connectivity itself from the edge stream so its view stays
    /// window-pure.
    fn add_edge(&mut self, edge: &Edge, out: &mut IngestDeltas) {
        let bt = edge.block_time as u64;

        let (src_idx, _) = self.intern_node(&edge.from_wallet, edge.slot);
        let (dst_idx, _) = self.intern_node(&edge.to_wallet, edge.slot);

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
            kind: kind.clone(),
        };

        let edge_id = self.alloc_edge_slot(graph_edge);

        let mint_str = mint_idx
            .map(|midx| self.mint_interner.lookup(midx).unwrap_or("").to_string());

        // Insert into every window whose cutoff this edge satisfies and
        // emit per-window NodeAdded (on 0->1 transition) + EdgeAdded so
        // each subscriber sees only what fits in its view.
        for w in 0..NUM_WINDOWS {
            let cutoff = self.latest_block_time.saturating_sub(WINDOWS[w]);
            if bt < cutoff {
                continue;
            }
            self.windows[w].edges_by_time.insert(edge_id, bt, &self.edges);

            let src_count = self.windows[w].edge_count_per_node[src_idx as usize];
            if src_count == 0 {
                let seq = self.next_seq();
                out.per_window[w].push(GraphDelta::NodeAdded {
                    seq,
                    idx: src_idx,
                    pubkey: edge.from_wallet.clone(),
                });
            }
            self.windows[w].edge_count_per_node[src_idx as usize] = src_count.saturating_add(1);

            if dst_idx != src_idx {
                let dst_count = self.windows[w].edge_count_per_node[dst_idx as usize];
                if dst_count == 0 {
                    let seq = self.next_seq();
                    out.per_window[w].push(GraphDelta::NodeAdded {
                        seq,
                        idx: dst_idx,
                        pubkey: edge.to_wallet.clone(),
                    });
                }
                self.windows[w].edge_count_per_node[dst_idx as usize] =
                    dst_count.saturating_add(1);
            }

            let seq = self.next_seq();
            out.per_window[w].push(GraphDelta::EdgeAdded {
                seq,
                idx: edge_id.idx,
                generation: edge_id.generation,
                src: src_idx,
                dst: dst_idx,
                mint: mint_str.clone(),
                amount: edge.amount,
                slot: edge.slot,
                kind: kind.clone(),
            });
        }

        self.out_adj[src_idx as usize].push(edge_id);
        self.in_adj[dst_idx as usize].push(edge_id);

        // Union-Find merge. Updates `node_to_component` for stats
        // and future in-process consumers; no event is emitted
        // because the frontend tracks connectivity itself from the
        // edge stream.
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

            // O(N) scan over node_to_component to relabel the smaller
            // component's members with the larger's id.
            for i in 0..self.node_to_component.len() {
                if self.node_to_component[i] == smaller_cid {
                    self.node_to_component[i] = larger_cid;
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

    /// Iterate `(src, dst, amount, kind)` tuples for every live edge in
    /// window `window_idx`. Walks the window's `edges_by_time` deque,
    /// filtering out stale slab handles (slot reused under a newer
    /// generation). Used by analytics snapshotting; callers hold the
    /// read lock for the duration of the iterator so the deque cannot
    /// mutate.
    pub fn iter_window_edges(
        &self,
        window_idx: usize,
    ) -> impl Iterator<Item = (NodeIdx, NodeIdx, u64, Option<EdgeKind>)> + '_ {
        self.windows[window_idx]
            .edges_by_time
            .iter()
            .filter_map(|id| {
                self.get_edge(id)
                    .map(|e| (e.src, e.dst, e.amount, e.kind.clone()))
            })
    }

    /// Lookup pubkey for a NodeIdx. Wrapper over the interner so callers
    /// outside `graph::` (e.g. analytics) don't need access to internals.
    pub fn lookup_pubkey(&self, idx: NodeIdx) -> Option<&str> {
        self.interner.lookup(idx)
    }

    /// Reverse lookup: pubkey -> NodeIdx if currently interned. Used by
    /// the agent's `wallet_profile` primitive to translate a model-supplied
    /// addr into the indexed identifier the snapshot helpers use.
    pub fn lookup_idx(&self, pubkey: &str) -> Option<NodeIdx> {
        self.interner.lookup_idx(pubkey)
    }

    /// True if at least one edge in window `window_idx` carries
    /// `mint_pubkey` as its mint. Allowlist gate for
    /// `/primitive/get_token_info`: only mints actually transferred
    /// inside the agent's analytic surface are eligible for on-chain
    /// metadata resolution. Out-of-window mints get rejected, which
    /// bounds outbound `getAccountInfo` calls to mints we already
    /// narrate live activity for and removes the "free oracle on
    /// arbitrary chain state" abuse shape.
    ///
    /// Returns `false` when the pubkey was never interned (mint never
    /// observed by ingest) or when no live edge in the window points
    /// at it. Walks the window's `edges_by_time` deque; cost is O(W)
    /// in window size, which the get_token_info handler pays once per
    /// request. Worst-case current load is ~10k edges in the 60s
    /// window, dominated by the on-chain RPC the gate authorizes.
    pub fn has_window_mint(&self, window_idx: usize, mint_pubkey: &str) -> bool {
        let Some(mint_idx) = self.mint_interner.lookup_idx(mint_pubkey) else {
            return false;
        };
        self.windows[window_idx]
            .edges_by_time
            .iter()
            .any(|id| {
                self.get_edge(id)
                    .and_then(|e| e.mint)
                    .is_some_and(|m| m == mint_idx)
            })
    }

    /// Is there at least one live edge in either direction between
    /// `a` and `b`? Used to detect first-edge-for-this-pair so we can
    /// bump unique_degree only when the pair becomes connected.
    pub(super) fn has_edge_between(&self, a: NodeIdx, b: NodeIdx) -> bool {
        if (a as usize) >= self.out_adj.len() || (b as usize) >= self.in_adj.len() {
            return false;
        }
        for &id in &self.out_adj[a as usize] {
            if let Some(e) = self.get_edge(id) {
                if e.dst == b {
                    return true;
                }
            }
        }
        for &id in &self.in_adj[a as usize] {
            if let Some(e) = self.get_edge(id) {
                if e.src == b {
                    return true;
                }
            }
        }
        false
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

        // e1 lies below every window's cutoff at bt=5000, so each
        // window emits its own EdgeExpired and pair of NodeExpired.
        assert_eq!(edge_expired, NUM_WINDOWS, "one EdgeExpired per window");
        assert_eq!(node_expired, NUM_WINDOWS * 2, "AAA+BBB orphan in each window");
    }
}
