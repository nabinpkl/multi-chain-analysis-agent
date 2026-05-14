
# Why Reads Need Locks — and the Lock-Free Alternative

You're asking the right question. Let me unpack what actually goes wrong without locks, and whether stale-read-tolerance saves us.

## What "stale" actually means in our state

GraphState fields modified concurrently:
- `interner.forward: FxHashMap<String, NodeIdx>` — hash table, internal pointers
- `interner.reverse: Vec<String>` — pointer + length + capacity
- `edges: Vec<Option<GraphEdge>>` — pointer + length + capacity
- `out_adj: Vec<Vec<EdgeIdx>>` — outer pointer + inner pointers
- `node_to_component: Vec<ComponentId>` — pointer + length
- `uf.parent: Vec<NodeIdx>`, `uf.rank: Vec<u8>` — pointer + length

These aren't just "logical" data — they're heap allocations with **internal invariants** (Vec capacity vs len, HashMap bucket arrays, raw pointers).

## What goes wrong without a lock

It's not "stale data" risk. It's **memory unsafety**.

Concrete failure: writer calls `edges.push(...)`. Vec is at capacity. Vec allocates a new larger buffer, copies elements, frees old buffer. During this:

- Old pointer freed
- Reader concurrently does `edges[idx]` → reads from freed memory → **use-after-free**, segfault, or torn read of garbage

Or: HashMap rehashes during insert. Reader's bucket pointer now points into the half-filled new bucket array. Reads garbage.

Or even simpler: Vec push grows length. Reader reads `len` then indexes. Between those two reads, a different write changes `len` AND the underlying buffer. Reader indexes a newly-allocated slot that hasn't been initialized yet. **UB.**

Rust's borrow checker prevents this at compile time by refusing to let two pieces of code hold non-Send `&mut` and `&` to the same data. Locks are how we tell the compiler "trust us, runtime coordination." Drop the lock = compile error or `unsafe` block, and `unsafe` only works if we ACTUALLY synchronize somehow else.

So: locks aren't about logical staleness, they're about **structural integrity of the data structures while pointers are being shuffled.**

## "But stale reads are fine" — what would that look like

If "stale" means "I'm OK reading state that's a few ms behind," there are real lock-free patterns:

### 1. Persistent / immutable snapshots (RCU-style)

Wrap state in `Arc<GraphState>`. Writers never mutate in place. To update: clone the entire state, mutate the clone, atomically swap the `Arc`. Readers hold their `Arc` for as long as they need; writes don't block them.

```rust
state: ArcSwap<GraphState>
```

Reader: `let snap = state.load();` — gets a snapshot Arc. Walks it freely. Writer's swap doesn't affect snap.

**Cost**: every write clones the entire state. For ~100k node graph, that's ~100MB / write × 100 writes/sec = 10GB/sec memcpy. Dead.

Persistent data structures (immutable HashMap/Vec via structural sharing — `im` crate or `rpds`) make this cheap by sharing common parts. Each write copies O(log n) nodes, not O(n). Real win.

But: structural-sharing data structures are 5-20x slower constant-factor than `Vec` and `FxHashMap`. We pay on hot paths (every ingest, every BFS) to make cold-start cheap. Wrong tradeoff.

### 2. Per-cold-start snapshot (the Arc<StateSnapshot> mitigation)

Single moment: writer clones the relevant fields under write lock briefly, hands `Arc` to bootstrap task, releases lock. Bootstrap iterates the Arc off-lock. Writer continues mutating original. This is the mitigation I wrote into the plan.

Cost: one O(N) clone per cold-start. ~10MB transient. Fine if cold-starts are rare.

### 3. Append-only structures with atomic length

If our data were strictly append-only (no slab reuse, no deletions, no mutation of existing entries), readers could read with just `Vec.len() as Atomic` and indexed reads would be safe — capacity-stable Vecs (with explicit reserve up-front) wouldn't reallocate.

But we have:
- Slab reuse (writes to existing indices)
- Adjacency vec mutations (push + retain on existing slots)
- HashMap inserts (rehashing)
- UF path compression (writes during reads!)

Append-only doesn't fit our access pattern. Pure write-once-read-many would, but we need updates.

### 4. Sharded locks

Split state into N shards by `NodeIdx % N`, lock per shard. Bootstrap acquires all N briefly, copies, releases. Real ingests only contend on the shard they touch. Adds complexity, marginal win at our scale.

## What about UF path compression on reads?

Subtle: `find(x)` mutates parent[] (path compression). It "reads" the component-of-x but writes parent[] as a side effect. So in our design **reads are not actually read-only** — a "read query" through UF does writes.

This forces RwLock writes for UF queries, OR we drop path compression on the read path (slower but lock-friendlier).

Workaround if we want lock-free reads: separate `node_to_component` lookups (read-only) from UF `find` calls (read-write). The plan already does this — `node_to_component` is the externally-visible identity, UF is internal-only for unions. So readers (HTTP handlers, bootstrap) only touch `node_to_component`, never UF. **That's already the design.** Good.

## So why still RwLock?

Even with UF mutation isolated, readers still touch:
- `interner.reverse: Vec<String>` (resolve NodeIdx → pubkey for bootstrap)
- `edges: Vec<Option<GraphEdge>>` (iterate non-tombstoned for bootstrap)
- `node_to_component: Vec<ComponentId>` (bootstrap + stats endpoint)
- `out_adj: Vec<Vec<EdgeIdx>>` (some queries)

All of these can have their backing buffer reallocated by writer (push past capacity, rehash). Without a lock, reader can hit use-after-free. Memory-unsafe.

RwLock is correct. Cheap when uncontended. Reader/reader concurrent. Reader/writer = brief wait.

## The real question — which write actually blocks bootstrap?

RwLock semantics:
- Many readers OR one writer, not both
- Reader holds = writer waits
- Writer holds = readers wait

When bootstrap is reading (read lock), the next ingest's `ingest()` write call blocks until bootstrap releases. This is the "stalls ingest" concern in risk #1.

For solo VM:
- Bootstrap = few hundred ms
- Ingest blocked for that duration
- Ingest rate ~10-100/sec → 1-30 ingests delayed
- Once bootstrap releases, queue drains in milliseconds

**Functionally fine.** That's why I wrote "Accept" in risk #1.

## Verdict

Lock isn't there to enforce "fresh reads." It's there to enforce **memory safety while shared data structures are being mutated**. Stale-read tolerance doesn't help — the failure mode without locks is segfault, not stale numbers.

Lock-free options exist (RCU, persistent ds, sharding) but each costs more than RwLock at our scale. RwLock is the right call. Risk #1's "accept" stance is correct. The Arc<StateSnapshot> mitigation is a precise tool we can deploy if logs ever show ingest-stall during cold-start, not before.

Want to bake any of this clarification into the plan, or leave it as conversation context?


## 1. The Core Problem: Structural Integrity
The text argues that the danger isn't "reading an old value," but "reading a pointer that no longer exists."

### The "Vector Reallocation" Failure
When a `Vec` (or any dynamic array) grows, it performs a specific dance:
1. **Allocate** a new, larger block of memory.
2. **Copy** data from the old block to the new one.
3. **Free** the old block.



If a Reader is looking at the `edges` list while a Writer is pushing data, the Reader might be holding a pointer to the **old block**. The moment the Writer frees that block, the Reader is suddenly pointing at "garbage" or unallocated memory. This leads to:
* **Segmentation Faults:** The OS kills the program for touching forbidden memory.
* **Undefined Behavior (UB):** The program continues but reads random bits, leading to corrupted state or security vulnerabilities.

---

## 2. Why "Stale Reads" Don't Solve This
If you just "tolerated" stale reads without a lock, you aren't reading an old version of the graph; you are reading a **half-destroyed** version of the graph's internal plumbing. 

The text highlights that **Rust’s Borrow Checker** is the "police officer" here. It won't even let you compile code that allows a reader and writer to touch the same memory at the same time unless you use a synchronization primitive (like `RwLock` or `Mutex`) or `unsafe` blocks.

---

## 3. The Alternatives (and why they were rejected)

| Strategy | How it works | Why it was rejected here |
| :--- | :--- | :--- |
| **RCU / Atomic Swap** | Copy the whole state, update the copy, swap a pointer. | **Too slow/heavy.** 100MB per write is too much "garbage" for the CPU to move around. |
| **Persistent Data Structures** | Use trees where nodes are shared between versions. | **Poor cache locality.** These are 5-20x slower for standard lookups than a simple `Vec`. |
| **Append-Only** | Never delete or move; only add to the end. | **Incompatible.** The graph needs to reuse slots (slabs) and update Union-Find structures. |
| **Sharded Locks** | Break the graph into 16 pieces; lock only the piece you need. | **Overkill.** Adds massive complexity for a performance gain that isn't needed at this scale. |

---

## 4. The Union-Find (UF) "Gotcha"
This is a subtle point: in a Union-Find structure, even a **read** (finding which group a node belongs to) often performs a **write** (Path Compression). 



If the "Reader" is technically writing to the `parent` array to optimize it, they need a **Write Lock**, which defeats the purpose of an `RwLock` (Read-Write Lock). The solution mentioned is to use a separate `node_to_component` map for readers so they don't trigger that internal optimization.

---

## 5. The Verdict: Why `RwLock` Wins
The `RwLock` is the "boring but correct" choice. 
* **Concurrent Reads:** Multiple readers (like a bootstrap process and a health check) can run at the same time.
* **Writer Priority:** A writer only blocks readers for the few milliseconds it takes to update the pointers.
* **The "Stall" is Acceptable:** Even if an ingest is delayed by 200ms during a bootstrap, the system will just buffer that ingest and catch up instantly once the lock is released.

### Summary for the Plan
This should definitely stay as **conversation context**. It justifies why you aren't over-engineering a lock-free solution. It proves that the "risk" of blocking an ingest is a calculated trade-off to ensure the system doesn't crash.
