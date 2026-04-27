use rustc_hash::FxHashMap;

pub type NodeIdx = u32;

/// Slab-based string interner. Supports freeing entries via a free-list so
/// that interned NodeIdx values can be reused after a node is expired.
///
/// Invariants:
/// - `forward` maps live pubkeys to their current idx.
/// - `reverse[idx]` is `Some(pubkey)` if the slot is occupied, `None` if
///   freed and on the free-list.
/// - `free_slots` is a LIFO stack of freed indices.
#[derive(Default)]
pub struct NodeInterner {
    forward: FxHashMap<String, NodeIdx>,
    reverse: Vec<Option<String>>,
    free_slots: Vec<NodeIdx>,
}

impl NodeInterner {
    /// Returns `(idx, newly_inserted)`.
    pub fn intern(&mut self, pubkey: &str) -> (NodeIdx, bool) {
        if let Some(&idx) = self.forward.get(pubkey) {
            return (idx, false);
        }
        let idx = if let Some(slot) = self.free_slots.pop() {
            self.reverse[slot as usize] = Some(pubkey.to_string());
            slot
        } else {
            let idx = self.reverse.len() as NodeIdx;
            self.reverse.push(Some(pubkey.to_string()));
            idx
        };
        self.forward.insert(pubkey.to_string(), idx);
        (idx, true)
    }

    /// Free the slot for `idx`. Removes from `forward` map and marks the
    /// reverse slot as `None`. The slot goes onto the free-list.
    pub fn free(&mut self, idx: NodeIdx) {
        if let Some(pubkey) = self.reverse[idx as usize].take() {
            self.forward.remove(&pubkey);
            self.free_slots.push(idx);
        }
    }

    pub fn lookup(&self, idx: NodeIdx) -> Option<&str> {
        self.reverse
            .get(idx as usize)
            .and_then(|opt| opt.as_deref())
    }

    /// Reverse lookup: pubkey -> idx if currently interned.
    pub fn lookup_idx(&self, pubkey: &str) -> Option<NodeIdx> {
        self.forward.get(pubkey).copied()
    }

    /// Number of live (non-freed) interned entries.
    pub fn len(&self) -> u32 {
        self.forward.len() as u32
    }

    /// Total allocated slots (including freed tombstones). Used for
    /// bounds-checking adjacency lists.
    pub fn capacity(&self) -> u32 {
        self.reverse.len() as u32
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn intern_same_key_twice_returns_same_idx() {
        let mut interner = NodeInterner::default();
        let (idx1, new1) = interner.intern("wallet_abc");
        let (idx2, new2) = interner.intern("wallet_abc");
        assert_eq!(idx1, idx2);
        assert!(new1);
        assert!(!new2);
    }

    #[test]
    fn lookup_roundtrip() {
        let mut interner = NodeInterner::default();
        let (idx, _) = interner.intern("wallet_xyz");
        assert_eq!(interner.lookup(idx), Some("wallet_xyz"));
    }

    #[test]
    fn lookup_out_of_bounds_returns_none() {
        let interner = NodeInterner::default();
        assert!(interner.lookup(99).is_none());
    }

    #[test]
    fn multiple_keys_get_distinct_indices() {
        let mut interner = NodeInterner::default();
        let (a, _) = interner.intern("aaa");
        let (b, _) = interner.intern("bbb");
        let (c, _) = interner.intern("ccc");
        assert_ne!(a, b);
        assert_ne!(b, c);
        assert_eq!(interner.len(), 3);
    }

    #[test]
    fn free_list_reuse_intern_c_reuses_a_slot() {
        let mut interner = NodeInterner::default();
        let (idx_a, _) = interner.intern("A");
        let (_idx_b, _) = interner.intern("B");
        interner.free(idx_a);
        assert!(interner.lookup(idx_a).is_none());
        let (idx_c, new_c) = interner.intern("C");
        assert!(new_c);
        // C must reuse A's freed slot
        assert_eq!(idx_c, idx_a);
        assert_eq!(interner.lookup(idx_c), Some("C"));
        assert_eq!(interner.len(), 2); // B + C
    }
}
