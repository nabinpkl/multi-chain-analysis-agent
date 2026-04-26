use super::interner::NodeIdx;

pub struct MergeResult {
    pub absorbed_root: NodeIdx,
    pub surviving_root: NodeIdx,
}

#[derive(Default)]
pub struct UnionFind {
    parent: Vec<NodeIdx>,
    rank: Vec<u8>,
}

impl UnionFind {
    /// Add a new node as its own root. Returns the new node's idx.
    pub fn push_singleton(&mut self) -> NodeIdx {
        let idx = self.parent.len() as NodeIdx;
        self.parent.push(idx);
        self.rank.push(0);
        idx
    }

    /// Find root with path compression. Requires mutable self.
    pub fn find(&mut self, x: NodeIdx) -> NodeIdx {
        let mut root = x;
        // Find root without mutation first
        while self.parent[root as usize] != root {
            root = self.parent[root as usize];
        }
        // Path compression: point all nodes on the path directly to root
        let mut cur = x;
        while cur != root {
            let next = self.parent[cur as usize];
            self.parent[cur as usize] = root;
            cur = next;
        }
        root
    }

    /// Union by rank. Returns Some(MergeResult) if the two nodes were in
    /// different components, None if already same.
    pub fn union(&mut self, a: NodeIdx, b: NodeIdx) -> Option<MergeResult> {
        let ra = self.find(a);
        let rb = self.find(b);
        if ra == rb {
            return None;
        }
        let (absorbed_root, surviving_root) = if self.rank[ra as usize] < self.rank[rb as usize] {
            // ra absorbed into rb
            self.parent[ra as usize] = rb;
            (ra, rb)
        } else if self.rank[ra as usize] > self.rank[rb as usize] {
            // rb absorbed into ra
            self.parent[rb as usize] = ra;
            (rb, ra)
        } else {
            // equal rank: ra becomes root, bump rank
            self.parent[rb as usize] = ra;
            self.rank[ra as usize] += 1;
            (rb, ra)
        };
        Some(MergeResult { absorbed_root, surviving_root })
    }

    /// Count nodes where parent[i] == i (roots).
    pub fn count_roots(&self) -> u32 {
        self.parent
            .iter()
            .enumerate()
            .filter(|(i, p)| **p == *i as NodeIdx)
            .count() as u32
    }

    pub fn len(&self) -> usize {
        self.parent.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn three_nodes_union() {
        let mut uf = UnionFind::default();
        let n0 = uf.push_singleton();
        let n1 = uf.push_singleton();
        let n2 = uf.push_singleton();

        assert_eq!(uf.count_roots(), 3, "before any union, 3 roots");

        let merge = uf.union(n0, n1);
        assert!(merge.is_some(), "union(0,1) should merge");
        assert_eq!(uf.find(n0), uf.find(n1), "find(0) == find(1) after union");
        assert_eq!(uf.count_roots(), 2, "2 roots after first union");

        let merge2 = uf.union(n1, n2);
        assert!(merge2.is_some(), "union(1,2) should merge");
        assert_eq!(uf.find(n0), uf.find(n2), "all 3 same root");
        assert_eq!(uf.count_roots(), 1, "1 root after all unions");
    }

    #[test]
    fn union_same_component_returns_none() {
        let mut uf = UnionFind::default();
        let n0 = uf.push_singleton();
        let n1 = uf.push_singleton();
        uf.union(n0, n1);
        let result = uf.union(n0, n1);
        assert!(result.is_none(), "union of already-same component returns None");
    }

    #[test]
    fn path_compression_works() {
        let mut uf = UnionFind::default();
        // Create a chain: 0-1-2-3
        let n0 = uf.push_singleton();
        let n1 = uf.push_singleton();
        let n2 = uf.push_singleton();
        let n3 = uf.push_singleton();
        uf.union(n0, n1);
        uf.union(n1, n2);
        uf.union(n2, n3);
        let root = uf.find(n3);
        // After find, all should point to root
        assert_eq!(uf.find(n0), root);
        assert_eq!(uf.find(n1), root);
        assert_eq!(uf.find(n2), root);
        assert_eq!(uf.find(n3), root);
    }
}
