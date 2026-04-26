
# GraphDelta enum (final shape for slice 2)

```rust
#[derive(Serialize, TS)]
#[serde(tag = "type")]
pub enum GraphDelta {
    NodeAdded     { seq: u64, idx: u32, pubkey: String },
    EdgeAdded     { seq: u64, idx: u32, src: u32, dst: u32, mint: Option<String>,
                    amount: u64, slot: u64, kind: Option<EdgeKind> },
    ComponentSet  { seq: u64, node: u32, component_id: u64 },
    EdgeExpired   { seq: u64, idx: u32 },
    NodeExpired   { seq: u64, idx: u32 },
    CaughtUp      { seq: u64 },
}
```

# `ComponentAssigned` — Its Job

One job: **tell the client which component a node belongs to right now.**

That's it. Whenever the backend wants the client to update its mental model of "node N is in component C," it emits one of these.

## When backend emits it

Three triggers, all reduce to "node's component changed":

### 1. New node born
First time we see a node, it's its own component (no edges yet, or merging happens immediately after).
```
NodeAdded { idx: 42, pubkey: "..." }
ComponentAssigned { node: 42, component_id: 1001 }
```

### 2. Two components merge (during ingest)
Edge connects nodes in component 7 (size 3) and component 12 (size 100). Smaller side absorbed into larger. Each node from the smaller side gets reassigned.
```
ComponentAssigned { node: 42, component_id: 12 }
ComponentAssigned { node: 43, component_id: 12 }
ComponentAssigned { node: 44, component_id: 12 }
```
Three events for the three nodes that moved. Component 7 now has zero references, ceases to exist (implicit).

### 3. A component splits (during expiry)
An expiring tree edge breaks component 12 into two pieces. Largest piece keeps id 12; smaller piece gets fresh id 1502.
```
ComponentAssigned { node: 99, component_id: 1502 }
ComponentAssigned { node: 100, component_id: 1502 }
```

## What client does with it

One line of reducer:
```ts
case "ComponentAssigned":
  state.nodeToComponent.set(delta.node, delta.component_id);
  // re-color the node; layout/role hooks read this map next render
  break;
```

That's the entire client-side handling. No bookkeeping of "did this component exist before?" No reverse index. Just write to the map.

## Why it exists at all

Without it, the client has no way to know which nodes share a component. Edges alone don't tell you — two nodes connected by an edge MIGHT be in the same component, but components also include nodes connected via 2-hop, 3-hop, ... paths. Computing that client-side = running Union-Find on the client = exactly what slice 2 deletes.

So: backend computes connectivity (via UF + BFS-on-split), backend tells client per-node who-belongs-where, client just stores it.

## The simplest mental model

`ComponentAssigned` is **the protocol's way of saying "UPDATE node SET component_id = X WHERE node = N."** A column update on the client's node table. Backend is the database, client is a read replica, this delta is the row-level write that keeps replica in sync.

Same job a CDC (change-data-capture) row would play in a postgres replication stream.