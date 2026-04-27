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
