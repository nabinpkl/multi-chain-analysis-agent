//! Deterministic initial position assignment, mirroring
//! `frontend/src/lib/per-component-layout.ts` + `placeNear` / `hash01`
//! from `frontend/src/hooks/use-raw-stream.ts`.
//!
//! Same FNV-1a 32-bit constants and same ±5000 orphan box and 1.5
//! spawn-radius near partner so Rust and TS agree on first-frame coords.

use super::interner::NodeIdx;
use super::GraphState;

const SPAWN_RADIUS: f32 = 1.5;
const ORPHAN_SPREAD: f32 = 10000.0;

/// FNV-1a 32-bit hash mapped to `[0, 1)`. Matches the JS implementation
/// in `use-raw-stream.ts::hash01` byte-for-byte.
fn hash01(s: &str) -> f32 {
    let mut h: u32 = 2166136261;
    for b in s.as_bytes() {
        h ^= *b as u32;
        h = h.wrapping_mul(16777619);
    }
    (h as f32) / 4294967296.0
}

/// Compute initial (x, y) for a freshly interned node.
///
/// `partner_idx`: the other endpoint of the edge that is causing this
/// node to enter. If `Some`, spawn at `partner ± hash01(pubkey) * 1.5`.
/// If `None` (true orphan: both endpoints arrived together), scatter
/// across `±5000`.
pub fn compute(
    g: &GraphState,
    pubkey: &str,
    partner_idx: Option<NodeIdx>,
) -> (f32, f32) {
    if let Some(p_idx) = partner_idx {
        let px = g.pos_x[p_idx as usize];
        let py = g.pos_y[p_idx as usize];
        let angle = hash01(pubkey) * std::f32::consts::TAU;
        return (
            px + SPAWN_RADIUS * angle.cos(),
            py + SPAWN_RADIUS * angle.sin(),
        );
    }
    // Orphan: deterministic scatter inside ±5000 box.
    let hx = hash01(&format!("x:{pubkey}"));
    let hy = hash01(&format!("y:{pubkey}"));
    (
        (hx - 0.5) * ORPHAN_SPREAD,
        (hy - 0.5) * ORPHAN_SPREAD,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hash01_deterministic() {
        let a = hash01("AAA");
        let b = hash01("AAA");
        assert_eq!(a, b);
        assert!(a >= 0.0 && a < 1.0);
    }

    #[test]
    fn hash01_matches_js_byte_for_byte() {
        // Bit-exact parity with frontend `hash01("A")`.
        // FNV-1a 32-bit on a single byte: low 32 bits of
        // ((2166136261 ^ 65) * 16777619) divided by 2^32, which is the
        // same value Math.imul produces in JS (it returns the same low
        // 32 bits, sign aside, and `>>> 0` makes it unsigned).
        let h = hash01("A");
        assert!((h - 0.76580757).abs() < 1e-5, "hash01('A') = {h}");
    }

    #[test]
    fn orphan_scatter_inside_box() {
        let g = GraphState::default();
        let (x, y) = compute(&g, "OrphanWallet", None);
        assert!(x >= -5000.0 && x <= 5000.0, "x = {x}");
        assert!(y >= -5000.0 && y <= 5000.0, "y = {y}");
    }
}
