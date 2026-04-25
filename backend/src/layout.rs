//! Hub-partitioned tiled layout.
//!
//! Replaces force-directed simulation with a deterministic geometric
//! tiling:
//!   1. Classify every wallet as HUB or SPOKE by walking nodes in
//!      decreasing-degree order. The first node with no hub neighbor
//!      becomes a hub; every other node attaches to its strongest
//!      hub neighbor (the one it shares the highest-volume edge with).
//!      Neighbors that are themselves spokes resolve through their
//!      parent, so chains of low-degree wallets end up anchored to a
//!      real hub rather than bootstrapping spurious hubs.
//!   2. Each hub claims a disc-shaped tile sized by spoke count.
//!   3. Tiles are packed on a golden-angle spiral with origin-first
//!      ordering. For each subsequent tile, walk outward along its
//!      ray until it no longer overlaps any previously placed tile.
//!      This guarantees non-overlap by construction, which is the
//!      failure mode FA2 couldn't fix: two large hubs in the same
//!      component always interpenetrated each other's spoke clouds.
//!   4. Within a tile: hub at center, spokes evenly distributed on a
//!      halo circle at 72% of tile radius. Spoke order is sorted by
//!      id so angular positions are stable across ticks.
//!
//! Positions are a pure function of (nodes, edges). Topology changes
//! cause position jumps; the frontend's 500 ms tween animation smooths
//! them. There is no alpha, no iteration count, no force balance.

use std::collections::{HashMap, HashSet};
use std::time::Instant;

use crate::domain::{EdgeView, NodeView};

/// Fixed contribution to every tile's radius, regardless of spoke
/// count. Keeps solo hubs visible.
const HUB_RADIUS_BASE: f64 = 40.0;
/// Scale on sqrt(spoke_count). A 16-spoke hub gets radius 140; a
/// 100-spoke hub gets 290.
const HUB_RADIUS_PER_SQRT_SPOKE: f64 = 25.0;
/// Where the spoke halo sits inside the tile, as a fraction of the
/// tile's radius. 0.72 leaves room for inter-tile padding and label
/// rendering.
const SPOKE_HALO_FRACTION: f64 = 0.72;
/// Gap enforced between adjacent tiles during packing.
const TILE_PADDING: f64 = 25.0;
/// 137.507...° in radians. Golden-angle spiral keeps tile placement
/// from aligning on obvious axes.
const GOLDEN_ANGLE: f64 = 2.399_963_229_728_653;

#[derive(Debug, Clone)]
pub struct PositionEntry {
    pub x: f64,
    pub y: f64,
    pub last_seen: Instant,
}

#[derive(Debug, Default)]
pub struct PositionStore {
    entries: HashMap<String, PositionEntry>,
}

impl PositionStore {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn get(&self, id: &str) -> Option<&PositionEntry> {
        self.entries.get(id)
    }
}

/// Stamp (x, y) from the store onto each NodeView. Nodes not yet laid
/// out default to (0, 0); the next tick populates them.
pub fn stamp_positions(nodes: &mut [NodeView], store: &PositionStore) {
    for n in nodes.iter_mut() {
        if let Some(e) = store.entries.get(&n.id) {
            n.x = e.x;
            n.y = e.y;
        }
    }
}

/// Recompute positions for every node in the current hub-view and
/// replace the store contents. O((nodes + edges) + tiles²). At 300
/// nodes and ~30 tiles this is sub-millisecond.
pub fn advance(
    store: &mut PositionStore,
    nodes: &[NodeView],
    edges: &[EdgeView],
    now: Instant,
) {
    store.entries.clear();
    if nodes.is_empty() {
        return;
    }
    let positions = compute_layout(nodes, edges);
    for (id, (x, y)) in positions {
        store.entries.insert(
            id,
            PositionEntry {
                x,
                y,
                last_seen: now,
            },
        );
    }
}

fn compute_layout(nodes: &[NodeView], edges: &[EdgeView]) -> HashMap<String, (f64, f64)> {
    // Adjacency: wallet id -> [(neighbor_id, volume), ...]. Both
    // directions stored so classification is direction-agnostic.
    let mut adj: HashMap<&str, Vec<(&str, f64)>> = HashMap::new();
    for e in edges {
        adj.entry(e.from.as_str())
            .or_default()
            .push((e.to.as_str(), e.volume_sol));
        adj.entry(e.to.as_str())
            .or_default()
            .push((e.from.as_str(), e.volume_sol));
    }

    // Process nodes highest-degree first, breaking ties by id asc so
    // hub classification is reproducible across ticks and processes.
    let mut sorted: Vec<&NodeView> = nodes.iter().collect();
    sorted.sort_by(|a, b| {
        b.degree
            .cmp(&a.degree)
            .then_with(|| a.id.cmp(&b.id))
    });

    // Classify: first-seen node in degree order with no hub neighbor
    // becomes a hub; everyone else attaches to their strongest hub
    // neighbor (resolved one level through the parent map so a spoke
    // connected only to another spoke still finds the underlying hub).
    let mut hubs: HashSet<String> = HashSet::new();
    let mut parent: HashMap<String, String> = HashMap::new();
    for node in &sorted {
        let empty: Vec<(&str, f64)> = Vec::new();
        let nbrs = adj.get(node.id.as_str()).unwrap_or(&empty);
        let mut best: Option<(String, f64)> = None;
        for (nb, vol) in nbrs {
            let root = if hubs.contains(*nb) {
                Some((*nb).to_string())
            } else {
                parent.get(*nb).cloned()
            };
            if let Some(r) = root {
                match &best {
                    Some((_, v)) if *v >= *vol => {}
                    _ => best = Some((r, *vol)),
                }
            }
        }
        match best {
            Some((h, _)) => {
                parent.insert(node.id.clone(), h);
            }
            None => {
                hubs.insert(node.id.clone());
            }
        }
    }

    // Group spokes under each hub. Spokes sorted by id so their
    // angular slot within the tile is stable.
    let mut tile_spokes: HashMap<String, Vec<String>> = HashMap::new();
    for h in &hubs {
        tile_spokes.insert(h.clone(), Vec::new());
    }
    for (spoke, hub) in &parent {
        if let Some(v) = tile_spokes.get_mut(hub) {
            v.push(spoke.clone());
        }
    }

    let mut tiles: Vec<Tile> = hubs
        .iter()
        .map(|h| {
            let mut spokes = tile_spokes.remove(h).unwrap_or_default();
            spokes.sort();
            let k = spokes.len() as f64;
            let radius = HUB_RADIUS_BASE + HUB_RADIUS_PER_SQRT_SPOKE * k.sqrt();
            Tile {
                hub_id: h.clone(),
                spokes,
                radius,
                center: (0.0, 0.0),
            }
        })
        .collect();

    // Pack tiles: largest at origin, rest on a golden-angle spiral.
    // For tile i the initial radius is set so it tangents the origin
    // tile; if it collides with any already-placed tile, step outward
    // until it fits.
    tiles.sort_by(|a, b| {
        b.radius
            .partial_cmp(&a.radius)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.hub_id.cmp(&b.hub_id))
    });
    for i in 0..tiles.len() {
        if i == 0 {
            tiles[0].center = (0.0, 0.0);
            continue;
        }
        let theta = i as f64 * GOLDEN_ANGLE;
        let (ct, st) = (theta.cos(), theta.sin());
        let mut r = tiles[0].radius + tiles[i].radius + TILE_PADDING;
        let mut attempts = 0;
        loop {
            let px = r * ct;
            let py = r * st;
            let mut fits = true;
            for j in 0..i {
                let dx = px - tiles[j].center.0;
                let dy = py - tiles[j].center.1;
                let d = (dx * dx + dy * dy).sqrt();
                if d < tiles[i].radius + tiles[j].radius + TILE_PADDING {
                    fits = false;
                    break;
                }
            }
            if fits {
                tiles[i].center = (px, py);
                break;
            }
            r += 5.0;
            attempts += 1;
            if attempts > 400 {
                // Give up and place at last tried position. Should
                // never hit with realistic inputs; the safety cap
                // keeps a pathological graph from hanging the tick.
                tiles[i].center = (px, py);
                break;
            }
        }
    }

    // Emit: hub at tile center, spokes evenly spaced on the halo.
    let mut positions: HashMap<String, (f64, f64)> = HashMap::new();
    for tile in &tiles {
        positions.insert(tile.hub_id.clone(), tile.center);
        let halo_r = tile.radius * SPOKE_HALO_FRACTION;
        let n = tile.spokes.len() as f64;
        for (k, spoke) in tile.spokes.iter().enumerate() {
            let angle = if n > 0.0 {
                2.0 * std::f64::consts::PI * k as f64 / n
            } else {
                0.0
            };
            let sx = tile.center.0 + halo_r * angle.cos();
            let sy = tile.center.1 + halo_r * angle.sin();
            positions.insert(spoke.clone(), (sx, sy));
        }
    }
    positions
}

#[derive(Debug)]
struct Tile {
    hub_id: String,
    spokes: Vec<String>,
    radius: f64,
    center: (f64, f64),
}
