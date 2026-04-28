

## Plan: moving layout to a Web Worker

### What moves into the worker

- `stepPerComponentLayout` — physics math, pairwise repulsion, attraction, collision pass.
- `pushComponentsApart` — inter-component centroids and translations.
- The frontend `ComponentState` (your local Union-Find) — needed by the layout to know component scope.
- Per-component velocities map (currently a module-level `Map` in `per-component-layout.ts`).
- Eventually: `detectMpcClusters` (Louvain) and `classifyNodes`. Move these in a second pass; layout first.

### What stays on the main thread

- graphology instance (Sigma reads from it directly; can't be shared with a worker without dropping Sigma).
- Sigma rendering.
- SSE EventSource (browser limitation: SSE is fine in workers, but keeping it on main lets React update status counters easily).
- React state, status pill, side panel.
- `applyEdge` graph mutations to graphology (Sigma's authoritative source).
- The dirty-component set (computed on main where edges arrive).

### Key tension

Sigma reads `x, y, size, color, label` from graphology by attribute name. Graphology lives on the main thread and is not transferable. So the worker can't own positions directly. **The worker holds a parallel mirror of node ids → positions** in typed arrays, computes new positions, and ships back a `Float32Array` that the main thread writes onto graphology before Sigma's next render frame.

This is the canonical pattern: **graphology stays the rendering source of truth; the worker is a position-computing satellite.**

---

### Architecture

```
   ┌──────────────────────── main thread ─────────────────────────┐
   │  SSE → applyEdge → graphology (x,y,size,color,...)           │
   │           │                                                  │
   │           │ post (transferable):                             │
   │           │   { type: 'edgeBatch',                           │
   │           │     edges: [[srcIdx, dstIdx, weight], ...],      │
   │           │     newNodes: [[idx, hashSeed], ...] }           │
   │           ▼                                                  │
   │     worker.postMessage(...)                                  │
   │                                                              │
   │  on message from worker:                                     │
   │     positions: Float32Array(2N) → write to graphology        │
   │     Sigma renders next frame                                 │
   └──────────────────────────────────────────────────────────────┘

   ┌────────────────────── worker ───────────────────────────────┐
   │  owns:                                                      │
   │    Map<nodeIdx, slot> + Float32Array positions               │
   │    Float32Array velocities                                  │
   │    ComponentState (UF)                                      │
   │    edges as CSR (offsets[N+1], targets[E])                  │
   │    role/community overlay maps                              │
   │                                                             │
   │  RAF-driven layoutTick:                                     │
   │    for each dirty root:                                     │
   │      step physics into positions[] in-place                 │
   │    pushComponentsApart over all centroids                   │
   │    postMessage({ type: 'positions', buffer: positions      │
   │                  .slice() }) (or transfer)                  │
   └─────────────────────────────────────────────────────────────┘
```

---

### Identity scheme

Right now your main-thread `applyEdge` keys nodes by **pubkey string** (graphology node id) and edges by `${edgeIdx}:${gen}` (graphology edge key). Strings cross the postMessage boundary fine but are slow to hash and not transferable. The worker should switch to **integer indices**.

Use the backend's `NodeIdx` (already on the wire from `NodeAdded`) as the worker's primary node identity. Main thread keeps a `nodeIdx → slotIndex` map (slot in the worker's Float32Array). Messages carry only integers. Position writes back use a `slotIndex → pubkey` reverse map on the main thread to look up the graphology id for `setNodeAttribute`.

This means: main thread holds the pubkey↔nodeIdx mapping (already does, via `idxToPubkey`). Worker holds the nodeIdx↔slot mapping. Together they round-trip without ever sending strings to the worker after node creation.

---

### Message protocol

Four message types, all transferable, all integer-keyed.

**main → worker**
- `init`: `{ canvasWidth, canvasHeight }` (for ORPHAN_SPREAD scaling).
- `addNodes`: `Int32Array` of nodeIdxs, `Float32Array` of (initial x, initial y) pairs. Main computes spawn positions (it knows the partner) and ships them.
- `addEdges`: `Int32Array` of `[srcSlot, dstSlot, weight, isMegaHub]` quads. Plus the dirty roots (Int32Array of root slots) so worker knows what to step next.
- `removeEdges`: `Int32Array` of (srcSlot, dstSlot) pairs.
- `removeNodes`: `Int32Array` of slots.
- `setOverlay`: `Int32Array` mapping slot → community id, slot → role enum. Sent every 3s when the detect interval finishes.

**worker → main**
- `positions`: `Float32Array` of 2N floats `[x0, y0, x1, y1, ...]` plus a `Uint32Array` of slot ids matching it. Transferable. Main writes onto graphology with a tight loop.

The `positions` message is the only message worker→main, sent once per layout tick. Everything else is main→worker.

---

### What this fixes

- **Layout never blocks render.** Sigma's draw loop and your animation frames live on a thread that does no physics. 60fps becomes the floor, not the ceiling.
- **MPC detect / Louvain stop hitching.** Currently they freeze the frame for 100-300ms every 3s. After layout moves, the next obvious thing is moving these too into the same worker (or a second one).
- **You can use TypedArray-friendly algorithms** like Barnes-Hut quadtrees, GPU-style SoA loops, etc. inside the worker without worrying about graphology's hashmap overhead.
- **Memory pressure separates.** Worker holds dense numeric arrays; main holds graphology + Sigma. If main GC's, worker doesn't pause.

### What this complicates

- **Two sources of truth for positions** during the round-trip. Order: worker writes positions, posts to main, main writes to graphology, Sigma reads. There's a 1-frame lag between physics and render. Invisible to the user (the user sees physics from frame N rendered at frame N+1, which is exactly what they see today anyway because animation is async).
- **Initial node placement.** Currently `applyEdge → ensureNode → placeNear(graph, ...)` reads the partner's x/y from graphology. The worker doesn't have graphology. Two options:
   1. **Main computes initial position**, posts it in the `addNodes` message. Simple, keeps placement logic on the side that knows about graphology. Recommended.
   2. **Worker holds positions authoritatively**, main asks the worker for "current position of nodeIdx X" before placing the new node. Round-trip latency, more complex. Avoid.
- **Velocity persistence across reset.** Today `velocities` is a module-level Map cleared by `resetLayoutVelocities()`. Worker needs an explicit `reset` message; the existing reset path (window change, "Reset from now") posts it.
- **Reset/window switch races.** Today the cleanup function in the SSE useEffect closes EventSource synchronously before the new effect runs. With a worker, you also need to `worker.terminate()` and recreate, or send a `reset` message and ensure no in-flight `positions` from the old window apply to the new graph. Cleanest: terminate + recreate worker on reset, like you do with EventSource. Worker startup is ~5ms; not a UX concern.
- **Debugging.** `console.log` from the worker doesn't show in the same React DevTools frame. You'll be looking at two consoles. The browser DevTools "Sources" panel handles workers fine but it's a small mental tax.
- **Node/edge ordering.** A `removeNodes` message must process after `removeEdges` for the same nodes (graphology rule, mirrored in your worker mirror). The protocol enforces this by ordering within each `step` message.

---

### What stays observable

- `dirtyRootsRef` collection still happens on main where edges arrive, then is shipped in the `addEdges` message. The worker uses it directly for scoping.
- The status pill (`edgeCount`, `nodeCount`) still updates from graphology (main thread state), no change.
- Reset, window switch, "Reset from now" button: same UX, terminate+recreate worker.

---

### Migration sequence

Tackle in this order to keep the app working at every step.

1. **Extract `stepPerComponentLayout` into a pure module that takes typed arrays and returns positions.** No worker yet; just decouple from graphology. This is the hardest refactor; rest is plumbing. Verify behavior matches today's by running it in-place against graphology-derived inputs.
2. **Build the slot-index mapping on main.** Map nodeIdx → slot, slot → pubkey. Wire it next to `idxToPubkey`. Verify by mirroring positions into a parallel Float32Array and asserting it equals graphology positions every frame.
3. **Wire the message protocol synchronously (no actual worker yet).** Make a `LayoutClient` interface with two implementations: `InProcessLayout` (calls the pure module directly) and `WorkerLayout` (postMessage). Use `InProcessLayout` first; behavior should be identical.
4. **Swap to `WorkerLayout`.** Now physics actually runs off-thread. The position write loop on main is the only thing that touches graphology.
5. **Move detection (Louvain / MPC / roles) into the worker** as a second message round-trip. Main posts `runDetect`, worker computes, posts `setOverlay`. The 3s interval just sends a message instead of running on main.
6. **(Later) Replace your O(N²) repulsion with Barnes-Hut** inside the worker. Now that layout is isolated, this is a localized swap with no cross-thread implications.

Each step is independently shippable. After step 4 you already have the "Sigma stays at 60fps" win. Steps 5-6 are scaling, not unblocking.

---

### Cost and payoff

- Extraction (step 1): half a day.
- Slot mapping + protocol (steps 2-3): half a day.
- Worker swap (step 4): a few hours.
- Detection move (step 5): half a day.
- Barnes-Hut (step 6): half a day.

Total: ~2 days for the full migration. The first day gets you the biggest win (steps 1-4); day 2 is incremental.

---

### Decisions you need to make first

1. **One worker or two?** Layout + detection in the same worker is simplest and fine for now. Splitting later is mechanical.
2. **`ArrayBuffer` transfer or copy?** Transfer is faster (zero-copy) but the buffer becomes detached on the sender side. Worker can keep a permanent buffer, send a copy each frame; or double-buffer (two buffers, alternate). Double-buffer is the standard pattern. Pick this; complexity is small.
3. **Schema versioning.** The protocol is internal so you don't need wire compatibility. Just hand-typed `MessageMain` and `MessageWorker` discriminated unions. Skip ts-rs / schemas for the worker boundary.
4. **Where does role classification go?** Today roles are written to graphology node attributes in the 3s detect interval (`graph.setNodeAttribute(id, "color", colorForRole(role))`). After step 5, the worker computes roles and sends a `setOverlay` message; main writes the colors onto graphology. Tiny extra plumbing, normal.

---

### TL;DR

Worker owns: physics math, position arrays, velocity, UF, edge CSR, role/community overlays.
Main owns: graphology, Sigma, SSE, React, dirty-component collection, initial node placement.
Boundary: integer-keyed messages, Float32Array positions transferred back each frame.
Hardest part: extracting `stepPerComponentLayout` to be graphology-free. After that, the worker swap is plumbing.
Result: 60fps becomes the floor, MPC detect stops hitching, scaling past 50k becomes feasible.
