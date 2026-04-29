//! Stable global community-id assignment.
//!
//! Louvain returns a fresh local partition every tick (ids 0..k-1
//! restart from scratch). Naively forwarding those ids to the
//! frontend would cause a full re-color on every analytics tick even
//! when membership didn't change. We assign monotonic global ids and
//! match new local groups to previous global ids by membership
//! overlap, biggest-group-first, one-time use of each prior id.
//!
//! After matching, any unmatched local group gets a fresh id from a
//! caller-owned `next_id` counter so old ids never get reused for
//! different memberships.
use rustc_hash::{FxHashMap, FxHashSet};

use crate::graph::interner::NodeIdx;

/// Match this tick's local partition to last tick's global ids.
///
/// `local_partition`: `node_idx -> local_community_id` from Louvain.
/// `prev_global`:     `node_idx -> global_community_id` from prev tick
///                    (None on first run).
/// `next_id`:         caller-owned monotonic counter for fresh ids.
///
/// Returns: `node_idx -> stable_global_community_id` covering exactly
/// the keys of `local_partition`.
pub fn stable_match(
    local_partition: &FxHashMap<NodeIdx, u32>,
    prev_global: Option<&FxHashMap<NodeIdx, u32>>,
    next_id: &mut u32,
) -> FxHashMap<NodeIdx, u32> {
    // Group local_partition by local id -> members.
    let mut groups: FxHashMap<u32, Vec<NodeIdx>> = FxHashMap::default();
    for (&node, &local_id) in local_partition.iter() {
        groups.entry(local_id).or_default().push(node);
    }

    // Sort groups by member count desc, then by smallest member NodeIdx
    // for determinism on ties.
    let mut ordered: Vec<(u32, Vec<NodeIdx>)> = groups.into_iter().collect();
    ordered.sort_by(|(_, a), (_, b)| {
        b.len()
            .cmp(&a.len())
            .then_with(|| a.iter().min().cmp(&b.iter().min()))
    });

    let mut local_to_global: FxHashMap<u32, u32> = FxHashMap::default();
    let mut used_prev: FxHashSet<u32> = FxHashSet::default();

    for (local_id, members) in &ordered {
        // Score overlap with each previous global id (only candidates
        // not yet matched in this pass).
        let chosen_prev = match prev_global {
            None => None,
            Some(prev) => {
                let mut overlap: FxHashMap<u32, usize> = FxHashMap::default();
                for &node in members {
                    if let Some(&pid) = prev.get(&node) {
                        if used_prev.contains(&pid) {
                            continue;
                        }
                        *overlap.entry(pid).or_insert(0) += 1;
                    }
                }
                // Pick the best non-zero overlap; tie-break by smaller
                // pid for determinism.
                overlap
                    .into_iter()
                    .filter(|&(_, n)| n > 0)
                    .max_by(|a, b| a.1.cmp(&b.1).then_with(|| b.0.cmp(&a.0)))
                    .map(|(pid, _)| pid)
            }
        };

        let assigned = if let Some(pid) = chosen_prev {
            used_prev.insert(pid);
            pid
        } else {
            let fresh = *next_id;
            *next_id += 1;
            fresh
        };
        local_to_global.insert(*local_id, assigned);
    }

    let mut out: FxHashMap<NodeIdx, u32> = FxHashMap::default();
    for (&node, &local_id) in local_partition.iter() {
        let gid = *local_to_global.get(&local_id).expect("local id always mapped");
        out.insert(node, gid);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_run_allocates_fresh_ids() {
        let mut local: FxHashMap<NodeIdx, u32> = FxHashMap::default();
        local.insert(0, 0);
        local.insert(1, 0);
        local.insert(2, 1);

        let mut next_id = 100u32;
        let out = stable_match(&local, None, &mut next_id);
        let unique: FxHashSet<u32> = out.values().copied().collect();
        assert_eq!(unique.len(), 2);
        // ids drawn from next_id sequence (100, 101)
        for &v in out.values() {
            assert!(v == 100 || v == 101);
        }
        assert_eq!(next_id, 102);
    }

    #[test]
    fn repeated_partition_keeps_global_ids() {
        // First tick: nodes 0,1,2 in community A; 3,4,5 in community B.
        let mut tick1: FxHashMap<NodeIdx, u32> = FxHashMap::default();
        for i in 0..3u32 {
            tick1.insert(i, 0);
        }
        for i in 3..6u32 {
            tick1.insert(i, 1);
        }

        let mut next_id = 0u32;
        let global1 = stable_match(&tick1, None, &mut next_id);

        // Second tick: same membership but Louvain returned different
        // local ids (group A is now local id 7, group B is local id 3).
        let mut tick2: FxHashMap<NodeIdx, u32> = FxHashMap::default();
        for i in 0..3u32 {
            tick2.insert(i, 7);
        }
        for i in 3..6u32 {
            tick2.insert(i, 3);
        }

        let global2 = stable_match(&tick2, Some(&global1), &mut next_id);

        // Every node keeps its global id.
        for i in 0..6u32 {
            assert_eq!(
                global2[&i], global1[&i],
                "node {} should retain its global id when membership doesn't change",
                i
            );
        }
    }

    #[test]
    fn no_double_assignment_of_prev_id() {
        // tick1: nodes 0,1 in comm 0; 2,3 in comm 1.
        let mut tick1: FxHashMap<NodeIdx, u32> = FxHashMap::default();
        tick1.insert(0, 0);
        tick1.insert(1, 0);
        tick1.insert(2, 1);
        tick1.insert(3, 1);
        let mut next_id = 0u32;
        let g1 = stable_match(&tick1, None, &mut next_id);

        // tick2: nodes 0,1,2 in one comm; 3 in another. The first
        // overlaps comm0 by 2 and comm1 by 1; takes comm0's id. The
        // second only overlaps comm1; should take comm1's id (still
        // unused by the first group).
        let mut tick2: FxHashMap<NodeIdx, u32> = FxHashMap::default();
        tick2.insert(0, 9);
        tick2.insert(1, 9);
        tick2.insert(2, 9);
        tick2.insert(3, 4);
        let g2 = stable_match(&tick2, Some(&g1), &mut next_id);
        let comm_a = g2[&0];
        let comm_b = g2[&3];
        assert_ne!(comm_a, comm_b);
        assert_eq!(comm_a, g1[&0], "merged group should inherit comm0's id");
        assert_eq!(comm_b, g1[&3], "leftover singleton should inherit comm1's id");
    }
}
