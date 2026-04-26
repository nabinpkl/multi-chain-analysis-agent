use rustc_hash::FxHashMap;

pub type NodeIdx = u32;

#[derive(Default)]
pub struct NodeInterner {
    forward: FxHashMap<String, NodeIdx>,
    reverse: Vec<String>,
}

impl NodeInterner {
    /// Returns `(idx, newly_inserted)`.
    pub fn intern(&mut self, pubkey: &str) -> (NodeIdx, bool) {
        if let Some(&idx) = self.forward.get(pubkey) {
            return (idx, false);
        }
        let idx = self.reverse.len() as NodeIdx;
        self.forward.insert(pubkey.to_string(), idx);
        self.reverse.push(pubkey.to_string());
        (idx, true)
    }

    pub fn lookup(&self, idx: NodeIdx) -> Option<&str> {
        self.reverse.get(idx as usize).map(|s| s.as_str())
    }

    pub fn len(&self) -> u32 {
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
}
