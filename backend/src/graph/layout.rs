//! Per-component force-directed layout. Verbatim Rust port of
//! `frontend/src/lib/per-component-layout.ts` so backend coords match
//! frontend coords visually for the same edge stream.
//!
//! Constants are intentionally identical to the JS file. Phase order is
//! identical: pairwise repulsion → edge attraction → integrate-with-
//! damping-and-clamp → 2-pass collision → push_components_apart.
//!
//! Tip-vs-non-tip and tip-vs-tip phases are stubs until backend tracks
//! node `role`. With every node defaulting to "normal" the tip loops
//! produce zero force  same outcome as the frontend in its current
//! pre-roles state.

use rustc_hash::FxHashMap;

use super::interner::NodeIdx;
use super::union_find::ComponentId;
use super::delta::PositionUpdate;
use super::GraphState;

const STEP_SCALE: f32 = 0.35;
const REPULSION: f32 = 300.0;
const ATTRACTION: f32 = 0.002;
const SIZE_POW: f32 = 0.9;
const MAX_N2_COMPONENT_SIZE: usize = 400;
const MIN_ACTIVE_SIZE: usize = 2;
const COLLISION_HUB_SIZE: f32 = 3.0;
const SIZE_TO_WORLD: f32 = 5.0;
const COLLISION_MARGIN: f32 = 8.0;
const VELOCITY_DAMPING: f32 = 0.7;
const MAX_STEP: f32 = 30.0;
const COMPONENT_PUSH_BUFFER: f32 = 400.0;
const MIN_COMPONENT_SIZE_FOR_PUSH: usize = 2;
// Per-tick cap on component-level rigid translation. Frontend has no
// cap (page reload resets state), but backend persists state across
// reloads, so without this an N-component overlap accumulates pushes
// additively and coordinates explode to 1e9+ within minutes. 200 is
// generous (about 6× MAX_STEP) so legitimate separations still resolve
// in a small number of ticks without runaway.
const MAX_COMPONENT_TRANSLATE_PER_TICK: f32 = 200.0;
const MEGAHUB_VISIBLE_DEGREE: u32 = 50;
const MEGAHUB_EDGE_REST_LENGTH: f32 = 420.0;
const LARGE_COMPONENT_SIZE: usize = 100;
const LARGE_COMPONENT_EDGE_REST_LENGTH: f32 = 90.0;

// Sizes are read from `GraphState::size` (degree-driven). The physics
// floor `Math.max(1, size)` from the JS port is applied below in the
// per-component loop so small (size=0.8) nodes still claim 1.0 of
// "personal space" in repulsion + collision math.
const SIZE_FLOOR: f32 = 1.0;

impl GraphState {
    /// Run one physics step across all components. Returns the set of
    /// (idx, x, y) updates whose position differs from the pre-tick
    /// snapshot. Caller broadcasts a single `PositionsBatch` delta if
    /// the result is non-empty.
    pub fn step_layout(&mut self) -> Vec<PositionUpdate> {
        // 0. Group live nodes by component_id.
        let mut per_component: FxHashMap<ComponentId, Vec<NodeIdx>> =
            FxHashMap::default();
        for (i, &cid) in self.node_to_component.iter().enumerate() {
            if cid == u64::MAX {
                continue;
            }
            // Belt-and-braces: also confirm interner hasn't freed it.
            if self.interner.lookup(i as NodeIdx).is_none() {
                continue;
            }
            per_component.entry(cid).or_default().push(i as NodeIdx);
        }

        // Track changed nodes (idx -> latest x,y). Use map so
        // pushComponentsApart can overwrite earlier entries.
        let mut latest: FxHashMap<NodeIdx, (f32, f32)> = FxHashMap::default();
        // Snapshot of pre-tick positions per node we touched (for the
        // change-detection diff). Only nodes inside an MIN_ACTIVE
        // component get touched.
        let mut pre_x: FxHashMap<NodeIdx, f32> = FxHashMap::default();
        let mut pre_y: FxHashMap<NodeIdx, f32> = FxHashMap::default();

        for ids in per_component.values() {
            let n = ids.len();
            if n < MIN_ACTIVE_SIZE {
                continue;
            }

            // Local per-component arrays.
            let mut xs: Vec<f32> = Vec::with_capacity(n);
            let mut ys: Vec<f32> = Vec::with_capacity(n);
            let mut sizes: Vec<f32> = Vec::with_capacity(n);
            let mut degrees: Vec<u32> = Vec::with_capacity(n);
            let mut id_index: FxHashMap<NodeIdx, usize> = FxHashMap::default();

            for (i, &nidx) in ids.iter().enumerate() {
                id_index.insert(nidx, i);
                xs.push(self.pos_x[nidx as usize]);
                ys.push(self.pos_y[nidx as usize]);
                // Apply Math.max(1, size) floor identical to JS port.
                sizes.push(self.size[nidx as usize].max(SIZE_FLOOR));
                degrees.push(self.unique_degree[nidx as usize]);
                pre_x.insert(nidx, self.pos_x[nidx as usize]);
                pre_y.insert(nidx, self.pos_y[nidx as usize]);
            }

            let mut fx: Vec<f32> = vec![0.0; n];
            let mut fy: Vec<f32> = vec![0.0; n];

            // Phase 1: pairwise repulsion (skip for components > 400).
            if n <= MAX_N2_COMPONENT_SIZE {
                for i in 0..n {
                    for j in (i + 1)..n {
                        let dx = xs[j] - xs[i];
                        let dy = ys[j] - ys[i];
                        let d2 = dx * dx + dy * dy + 0.01;
                        let d = d2.sqrt();
                        let size_factor =
                            (sizes[i] * sizes[j]).powf(SIZE_POW);
                        // No Louvain on backend yet → no community boost.
                        let f = (REPULSION * size_factor) / d2;
                        let ux = dx / d;
                        let uy = dy / d;
                        fx[i] -= f * ux;
                        fy[i] -= f * uy;
                        fx[j] += f * ux;
                        fy[j] += f * uy;
                    }
                }
            }

            // Phase 2: edge attraction. Iterate via out_adj only, so
            // each edge is visited exactly once per component pass.
            for i in 0..n {
                let nidx = ids[i];
                for &eidx in &self.out_adj[nidx as usize] {
                    let edge = match &self.edges[eidx as usize] {
                        Some(e) => e,
                        None => continue,
                    };
                    let other = edge.dst;
                    let j = match id_index.get(&other) {
                        Some(&j) => j,
                        None => continue, // edge to outside-component or freed
                    };
                    if j == i {
                        continue; // self-loop
                    }
                    let dx = xs[j] - xs[i];
                    let dy = ys[j] - ys[i];
                    let d = (dx * dx + dy * dy).sqrt() + 0.01;
                    let weight: f32 = 1.0; // backend doesn't track weight yet
                    let is_megahub = degrees[i] >= MEGAHUB_VISIBLE_DEGREE
                        || degrees[j] >= MEGAHUB_VISIBLE_DEGREE;
                    let rest_length = if is_megahub {
                        MEGAHUB_EDGE_REST_LENGTH
                    } else if n >= LARGE_COMPONENT_SIZE {
                        LARGE_COMPONENT_EDGE_REST_LENGTH
                    } else {
                        0.0
                    };
                    let stretch = d - rest_length;
                    if stretch <= 0.0 {
                        continue;
                    }
                    let f = ATTRACTION * weight * stretch;
                    let ux = dx / d;
                    let uy = dy / d;
                    fx[i] += f * ux;
                    fy[i] += f * uy;
                    fx[j] -= f * ux;
                    fy[j] -= f * uy;
                }
            }

            // Phase 3 (tip-account forces): skipped  no role data.

            // Phase 4: integrate with damping + MAX_STEP clamp.
            for i in 0..n {
                let nidx = ids[i];
                let mut vx = self.vel_x[nidx as usize] * VELOCITY_DAMPING
                    + fx[i] * STEP_SCALE;
                let mut vy = self.vel_y[nidx as usize] * VELOCITY_DAMPING
                    + fy[i] * STEP_SCALE;
                let speed = (vx * vx + vy * vy).sqrt();
                if speed > MAX_STEP {
                    let scale = MAX_STEP / speed;
                    vx *= scale;
                    vy *= scale;
                }
                self.vel_x[nidx as usize] = vx;
                self.vel_y[nidx as usize] = vy;
                xs[i] += vx;
                ys[i] += vy;
            }

            // Phase 5: 2-pass hub-vs-everything collision.
            let mut hub_indices: Vec<usize> = Vec::new();
            for i in 0..n {
                if sizes[i] >= COLLISION_HUB_SIZE {
                    hub_indices.push(i);
                }
            }
            for _pass in 0..2 {
                for &i in &hub_indices {
                    for j in 0..n {
                        if j == i {
                            continue;
                        }
                        resolve_overlap(i, j, &mut xs, &mut ys, &sizes);
                    }
                }
            }

            // Flush new positions to slabs.
            for i in 0..n {
                let nidx = ids[i];
                self.pos_x[nidx as usize] = xs[i];
                self.pos_y[nidx as usize] = ys[i];
                latest.insert(nidx, (xs[i], ys[i]));
            }
        }

        // Phase 6: pushComponentsApart (rigid-body translations).
        push_components_apart(self, &per_component, &mut latest);

        // Build the dirty list: only emit entries whose final position
        // diverges from the pre-tick snapshot.
        let mut out: Vec<PositionUpdate> = Vec::with_capacity(latest.len());
        for (idx, (x, y)) in latest {
            let px = pre_x.get(&idx).copied().unwrap_or(f32::NAN);
            let py = pre_y.get(&idx).copied().unwrap_or(f32::NAN);
            // f32::EPSILON guards against floating-point no-op writes;
            // anything bigger is a real move worth shipping.
            if (x - px).abs() > f32::EPSILON || (y - py).abs() > f32::EPSILON {
                out.push(PositionUpdate { idx, x, y });
            }
        }
        out
    }
}

fn resolve_overlap(
    i: usize,
    j: usize,
    xs: &mut [f32],
    ys: &mut [f32],
    sizes: &[f32],
) {
    let dx = xs[j] - xs[i];
    let dy = ys[j] - ys[i];
    let d2 = dx * dx + dy * dy + 0.0001;
    let d = d2.sqrt();
    let touch = (sizes[i] + sizes[j]) * SIZE_TO_WORLD + COLLISION_MARGIN;
    if d >= touch {
        return;
    }
    let overlap = touch - d;
    let total = sizes[i] + sizes[j];
    let share_i = sizes[j] / total;
    let share_j = sizes[i] / total;
    let ux = dx / d;
    let uy = dy / d;
    xs[i] -= ux * overlap * share_i;
    ys[i] -= uy * overlap * share_i;
    xs[j] += ux * overlap * share_j;
    ys[j] += uy * overlap * share_j;
}

struct Centroid {
    cid: ComponentId,
    x: f32,
    y: f32,
    size: usize,
    radius: f32,
}

fn push_components_apart(
    g: &mut GraphState,
    per_component: &FxHashMap<ComponentId, Vec<NodeIdx>>,
    latest: &mut FxHashMap<NodeIdx, (f32, f32)>,
) {
    let mut centroids: Vec<Centroid> = Vec::new();
    for (&cid, ids) in per_component {
        let size = ids.len();
        if size < MIN_COMPONENT_SIZE_FOR_PUSH {
            continue;
        }
        let mut cx = 0.0f32;
        let mut cy = 0.0f32;
        for &nidx in ids {
            cx += g.pos_x[nidx as usize];
            cy += g.pos_y[nidx as usize];
        }
        cx /= size as f32;
        cy /= size as f32;
        let mut radius = 0.0f32;
        for &nidx in ids {
            let x = g.pos_x[nidx as usize];
            let y = g.pos_y[nidx as usize];
            // Use real node size (no floor here  matches JS which
            // reads attr.size directly without the Math.max(1, ...)).
            let node_size = g.size[nidx as usize];
            let d = ((x - cx).powi(2) + (y - cy).powi(2)).sqrt()
                + node_size * SIZE_TO_WORLD;
            if d > radius {
                radius = d;
            }
        }
        centroids.push(Centroid {
            cid,
            x: cx,
            y: cy,
            size,
            radius,
        });
    }

    let mut translations: FxHashMap<ComponentId, (f32, f32)> =
        FxHashMap::default();
    for i in 0..centroids.len() {
        for j in (i + 1)..centroids.len() {
            let a = &centroids[i];
            let b = &centroids[j];
            let dx = b.x - a.x;
            let dy = b.y - a.y;
            let d = (dx * dx + dy * dy).sqrt() + 0.0001;
            let required = a.radius + b.radius + COMPONENT_PUSH_BUFFER;
            if d >= required {
                continue;
            }
            let push = required - d;
            let ux = dx / d;
            let uy = dy / d;
            let total = (a.size + b.size) as f32;
            let share_a = b.size as f32 / total;
            let share_b = a.size as f32 / total;
            let ta = translations.entry(a.cid).or_insert((0.0, 0.0));
            ta.0 -= ux * push * share_a;
            ta.1 -= uy * push * share_a;
            let tb = translations.entry(b.cid).or_insert((0.0, 0.0));
            tb.0 += ux * push * share_b;
            tb.1 += uy * push * share_b;
        }
    }

    for (cid, (dx, dy)) in &translations {
        // Cap the per-tick rigid translation magnitude.
        let mag = (dx * dx + dy * dy).sqrt();
        let (capped_dx, capped_dy) = if mag > MAX_COMPONENT_TRANSLATE_PER_TICK {
            let scale = MAX_COMPONENT_TRANSLATE_PER_TICK / mag;
            (dx * scale, dy * scale)
        } else {
            (*dx, *dy)
        };
        if let Some(ids) = per_component.get(cid) {
            for &nidx in ids {
                g.pos_x[nidx as usize] += capped_dx;
                g.pos_y[nidx as usize] += capped_dy;
                latest.insert(
                    nidx,
                    (g.pos_x[nidx as usize], g.pos_y[nidx as usize]),
                );
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::Edge;

    fn edge(from: &str, to: &str, slot: u64) -> Edge {
        Edge {
            signature: format!("{from}_{to}_{slot}"),
            instruction_idx: 0,
            slot,
            block_time: slot as u32,
            from_wallet: from.to_string(),
            to_wallet: to.to_string(),
            amount: 1_000_000,
            mint: String::new(),
            kind: String::new(),
            version: 1,
        }
    }

    #[test]
    fn step_layout_moves_two_node_component() {
        let mut gs = GraphState::default();
        gs.ingest(&edge("AAA", "BBB", 1));
        let updates = gs.step_layout();
        // Two-node component: attraction pulls them, integration moves them.
        assert!(!updates.is_empty(), "expected position updates after a tick");
    }

    #[test]
    fn step_layout_empty_graph_returns_empty() {
        let mut gs = GraphState::default();
        let updates = gs.step_layout();
        assert!(updates.is_empty());
    }
}
