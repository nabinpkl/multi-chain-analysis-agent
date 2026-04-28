/**
 * Deterministic spawn / teleport math, shared by main thread and the
 * layout engine (which may run in a worker). Both sides must produce
 * byte-identical positions for the same inputs so the worker's
 * teleport-on-merge stays consistent with the graphology state main
 * paints during the same frame.
 */

/** Small jitter, not a real radius. New nodes spawn next to their
 *  partner so FA2 doesn't have to drag them across the canvas. */
export const SPAWN_RADIUS = 1.5;

/** Orphans scatter across this box so brand-new components start out
 *  far from every other component. Each id hashes to a stable (x, y)
 *  inside it. */
export const ORPHAN_SPREAD = 10000;

/** Deterministic 32-bit hash → [0, 1). FNV-1a. */
export function hash01(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0) / 4294967296;
}

/** Spawn position for a brand-new orphan: stable per-id scatter. */
export function orphanSpawn(id: string): { x: number; y: number } {
  const hx = hash01("x:" + id);
  const hy = hash01("y:" + id);
  return {
    x: (hx - 0.5) * ORPHAN_SPREAD,
    y: (hy - 0.5) * ORPHAN_SPREAD,
  };
}

/** Spawn position for a new node placed near a known partner. */
export function partnerSpawn(
  newId: string,
  partner: { x: number; y: number },
): { x: number; y: number } {
  const angle = hash01(newId) * Math.PI * 2;
  return {
    x: partner.x + SPAWN_RADIUS * Math.cos(angle),
    y: partner.y + SPAWN_RADIUS * Math.sin(angle),
  };
}

/** Position for a member of the smaller component being migrated to
 *  the anchor of the larger component on a Union-Find merge. The
 *  formula matches `migrateMembersToAnchor` in `use-raw-stream.ts`
 *  exactly so main and engine produce the same layout. */
export function teleportToAnchor(
  memberId: string,
  anchor: { x: number; y: number },
): { x: number; y: number } {
  const angle = hash01(memberId) * Math.PI * 2;
  const r = SPAWN_RADIUS * (1 + hash01("r:" + memberId));
  return {
    x: anchor.x + r * Math.cos(angle),
    y: anchor.y + r * Math.sin(angle),
  };
}
