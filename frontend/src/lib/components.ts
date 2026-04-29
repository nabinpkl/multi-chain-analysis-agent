/**
 * Incremental Union-Find over wallet ids. Tracks which nodes belong
 * to the same connected component so the layout can teleport a
 * newly-merged component next to its partner rather than waiting for
 * FA2 to drag them across the canvas.
 *
 * Member sets are materialized so we can iterate a component's nodes
 * without scanning the whole graph on merge.
 */

export interface ComponentState {
  parent: Map<string, string>;
  size: Map<string, number>;
  members: Map<string, Set<string>>;
}

export function createComponentState(): ComponentState {
  return {
    parent: new Map(),
    size: new Map(),
    members: new Map(),
  };
}

export function addNode(state: ComponentState, id: string): void {
  if (state.parent.has(id)) return;
  state.parent.set(id, id);
  state.size.set(id, 1);
  state.members.set(id, new Set([id]));
}

export function findRoot(state: ComponentState, id: string): string {
  let root = id;
  while (state.parent.get(root) !== root) {
    root = state.parent.get(root)!;
  }
  // Path compression.
  let node = id;
  while (node !== root) {
    const next = state.parent.get(node)!;
    state.parent.set(node, root);
    node = next;
  }
  return root;
}

export interface MergeResult {
  merged: boolean;
  winner: string;
  // Members of the losing component that were re-rooted under the
  // winner. Empty if no merge occurred (same component already).
  migrated: string[];
}

export function union(
  state: ComponentState,
  a: string,
  b: string,
): MergeResult {
  const rootA = findRoot(state, a);
  const rootB = findRoot(state, b);
  if (rootA === rootB) {
    return { merged: false, winner: rootA, migrated: [] };
  }
  const sizeA = state.size.get(rootA)!;
  const sizeB = state.size.get(rootB)!;
  // Smaller component loses; its members get re-rooted to the larger
  // and migrated visually.
  const [winner, loser] = sizeA >= sizeB ? [rootA, rootB] : [rootB, rootA];
  const loserMembers = state.members.get(loser)!;
  const winnerMembers = state.members.get(winner)!;
  for (const id of loserMembers) {
    state.parent.set(id, winner);
    winnerMembers.add(id);
  }
  state.size.set(winner, winnerMembers.size);
  state.members.delete(loser);
  state.size.delete(loser);
  return { merged: true, winner, migrated: [...loserMembers] };
}

export function componentSize(state: ComponentState, id: string): number {
  const root = findRoot(state, id);
  return state.size.get(root) ?? 1;
}

/**
 * Adjacency lookup: given a node id, returns an iterable of its
 * current neighbors in the *post-removal* graph. Provided by the
 * caller so this module stays agnostic of where adjacency lives
 * (graphology on main, plain Map<slot, Map<slot, weight>> in the
 * worker  worker passes a wrapper that stringifies slot ints).
 */
export type Adjacency = (id: string) => Iterable<string>;

/**
 * React to an edge removal between `src` and `dst`. If the removal
 * disconnected a previously-merged component (i.e. the edge was a
 * "bridge"), split the component by promoting one of its nodes to a
 * new root.
 *
 * Two-sided BFS: alternate one step from each endpoint. As soon as
 * one frontier touches the other's visited set, we know the
 * component is still connected and abort cheaply (cost roughly the
 * number of hops between the two endpoints). When the edge IS a
 * true bridge, whichever BFS exhausts first is by definition the
 * smaller side (its reachable set is fully enumerated); we reassign
 * just that side, keeping work proportional to the smaller half.
 *
 * Returns `true` if a split happened. Caller is expected to have
 * already updated the underlying adjacency (graphology / worker
 * adj map) so that `adj()` reflects the post-removal world.
 *
 * No-op if either endpoint isn't tracked or if they're not in the
 * same component to begin with (out-of-order delivery, double
 * delivery, or stale event for an already-removed pair).
 */
export function handleEdgeRemoved(
  state: ComponentState,
  src: string,
  dst: string,
  adj: Adjacency,
): boolean {
  if (src === dst) return false;
  if (!state.parent.has(src) || !state.parent.has(dst)) return false;
  const root = findRoot(state, src);
  if (findRoot(state, dst) !== root) return false;

  const visitedA = new Set<string>([src]);
  const queueA: string[] = [src];
  const visitedB = new Set<string>([dst]);
  const queueB: string[] = [dst];

  // Alternate one BFS step per side. The loop exits when either a
  // search exhausts its queue (smaller side fully enumerated) or one
  // search reaches a node the other has visited (still connected).
  while (queueA.length > 0 && queueB.length > 0) {
    const nextA = queueA.shift()!;
    for (const n of adj(nextA)) {
      if (visitedA.has(n)) continue;
      if (visitedB.has(n)) return false; // a met b: still connected
      visitedA.add(n);
      queueA.push(n);
    }
    if (queueA.length === 0) break;
    const nextB = queueB.shift()!;
    for (const n of adj(nextB)) {
      if (visitedB.has(n)) continue;
      if (visitedA.has(n)) return false; // b met a: still connected
      visitedB.add(n);
      queueB.push(n);
    }
  }

  // The exhausted side is the smaller half. Pick a new root from it.
  // Preferring src/dst keeps future findRoot path-compression cheap
  // for the endpoint that triggered the split.
  const smaller = queueA.length === 0 ? visitedA : visitedB;
  const newRoot = smaller.has(src) ? src : dst;

  const oldMembers = state.members.get(root);
  if (!oldMembers) return false; // defensive; root should always have members

  const newMembers = new Set<string>();
  for (const id of smaller) {
    state.parent.set(id, newRoot);
    oldMembers.delete(id);
    newMembers.add(id);
  }
  state.members.set(newRoot, newMembers);
  state.size.set(newRoot, newMembers.size);
  state.size.set(root, oldMembers.size);
  // If newRoot used to be a non-root member, its old parent pointer
  // is now stale. Setting parent[newRoot] = newRoot makes findRoot
  // terminate correctly at it.
  state.parent.set(newRoot, newRoot);
  return true;
}

/**
 * Remove `id` from the union-find structure. Used when the backend
 * emits `NodeExpired` for the active window so the layout pipeline
 * doesn't keep iterating a dead pubkey in `members` and crash on
 * `graph.getNodeAttribute`.
 *
 * If `id` is the current root and the component has other members,
 * we rotate the root onto one of the survivors and re-point every
 * remaining member's `parent` directly at the new root. That's O(M)
 * per removal where M is the component size; acceptable because
 * NodeExpired is rate-limited by edge expiry.
 *
 * No-op if `id` was never tracked.
 */
export function removeNode(state: ComponentState, id: string): void {
  if (!state.parent.has(id)) return;
  const root = findRoot(state, id);
  const members = state.members.get(root);

  if (root !== id) {
    // Easy case: we're removing a non-root member.
    state.parent.delete(id);
    if (members) {
      members.delete(id);
      if (members.size === 0) {
        state.members.delete(root);
        state.size.delete(root);
      } else {
        state.size.set(root, members.size);
      }
    }
    return;
  }

  // Removing the root. If we're the only member, just delete the
  // component entirely.
  if (!members || members.size <= 1) {
    state.parent.delete(id);
    state.members.delete(id);
    state.size.delete(id);
    return;
  }

  // Multiple members: pick a survivor, re-root onto it.
  let newRoot: string | null = null;
  for (const m of members) {
    if (m !== id) {
      newRoot = m;
      break;
    }
  }
  if (newRoot === null) {
    // Defensive: should be unreachable since members.size > 1.
    state.parent.delete(id);
    state.members.delete(id);
    state.size.delete(id);
    return;
  }
  members.delete(id);
  for (const m of members) {
    state.parent.set(m, newRoot);
  }
  state.parent.delete(id);
  state.members.delete(id);
  state.size.delete(id);
  state.members.set(newRoot, members);
  state.size.set(newRoot, members.size);
}
