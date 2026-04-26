# Handling when edges expire i.e removing from Vector and why we need reindexing? Three Options

## Option 1: `Vec::remove(idx)` — shifts everything

Removing element at idx 5 shifts elements 6, 7, 8... down by one. Every stored `EdgeIdx ≥ 5` is now wrong. `out_adj` and `in_adj` are full of broken references. Must walk every adjacency list and renumber every value. O(E) per delete. **Reject.**

## Option 2: `Vec::swap_remove(idx)` — swap last into hole

Removing element at idx 5: take the last element (idx N-1), put it at idx 5, truncate. Only ONE EdgeIdx changes (the moved one). Walk `out_adj[moved.src]` and `in_adj[moved.dst]` to update the one reference. Cheap.

But: external holders of `EdgeIdx` (frontend, sitting on `EdgeAdded { idx: 99 }`) get out of sync when idx 99 silently becomes a different edge. Need to broadcast a `Reindex { from: 99, to: 5 }` event. New event type, new client code, ordering invariants. **Reject — complexity buys nothing.**

That's where the "reindexing" reference came from. You're right to push back. Better path:

## Option 3: Tombstone with free-list — what we actually want

Slot stays at same index. Content marked None. **EdgeIdx stays stable.** Free slots tracked in a free-list. New edges fill old slots first, only grow Vec when free-list empty.

```rust
struct GraphState {
    edges: Vec<Option<GraphEdge>>,
    free_edge_slots: Vec<EdgeIdx>,
    // ...
}

fn alloc_edge_slot(&mut self, edge: GraphEdge) -> EdgeIdx {
    if let Some(idx) = self.free_edge_slots.pop() {
        self.edges[idx as usize] = Some(edge);
        return idx;
    }
    let idx = self.edges.len() as EdgeIdx;
    self.edges.push(Some(edge));
    idx
}

fn free_edge_slot(&mut self, idx: EdgeIdx) {
    self.edges[idx as usize] = None;
    self.free_edge_slots.push(idx);
}
```

Properties:
- **Memory bounded** at peak concurrent edges in window. Doesn't leak.
- **No `Reindex` event.** EdgeIdx never moves while an edge lives there.
- **Reuse safe across SSE clients** because event order is preserved: `EdgeExpired { idx: 99 }` arrives before any later `EdgeAdded { idx: 99 }` (slot reused). Frontend applies in order, drops then adds. No confusion.
- **No background compaction task.** No `Reindex` broadcast.

Same allocator pattern we already use for component IDs. Slab allocator. Standard pattern.

## Cost

Same memory as plain Vec at steady state. ~16 extra bytes per Option discriminant (or zero if we niche-optimize via NonZeroU64 in GraphEdge fields, but fine to ignore for now). Free-list = one extra Vec<u32>, trivial.

## What changes in plan

Drop "tombstones grow unbounded, defer compaction" language. Replace with slab allocator + free-list. Want me to update the plan section?