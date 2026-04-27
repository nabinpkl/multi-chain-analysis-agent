use super::interner::NodeIdx;

pub type ComponentId = u64;

pub struct MergeResult {
    /// The root that was absorbed (smaller side).
    pub absorbed_root: NodeIdx,
    /// The root that survived (larger or equal side).
    pub surviving_root: NodeIdx,
    /// Component id on the surviving root (the one that persists).
    pub surviving_component_id: ComponentId,
    /// Component id that was on the absorbed root (now dead).
    pub absorbed_component_id: ComponentId,
}

/// Per-root metadata, colocated with parent/rank so union can read size
/// without a separate HashMap lookup.
#[derive(Clone, Copy, Default)]
struct RootMeta {
    component_id: ComponentId,
    size: u32,
}

#[derive(Default)]
pub struct UnionFind {
    parent: Vec<NodeIdx>,
    rank: Vec<u8>,
    meta: Vec<RootMeta>,
}

impl UnionFind {
    /// Add a new node as its own root with `component_id`. Returns the new
    /// node's idx.
    pub fn push_singleton(&mut self, component_id: ComponentId) -> NodeIdx {
        let idx = self.parent.len() as NodeIdx;
        self.parent.push(idx);
        self.rank.push(0);
        self.meta.push(RootMeta { component_id, size: 1 });
        idx
    }

    /// Find root with path compression.
    pub fn find(&mut self, x: NodeIdx) -> NodeIdx {
        let mut root = x;
        while self.parent[root as usize] != root {
            root = self.parent[root as usize];
        }
        let mut cur = x;
        while cur != root {
            let next = self.parent[cur as usize];
            self.parent[cur as usize] = root;
            cur = next;
        }
        root
    }

    /// Find root without path compression (for read-only contexts).
    pub fn find_immut(&self, x: NodeIdx) -> NodeIdx {
        let mut root = x;
        while self.parent[root as usize] != root {
            root = self.parent[root as usize];
        }
        root
    }

    /// Union by size. Returns `Some(MergeResult)` if the two nodes were in
    /// different components, `None` if already the same.
    pub fn union(&mut self, a: NodeIdx, b: NodeIdx) -> Option<MergeResult> {
        let ra = self.find(a);
        let rb = self.find(b);
        if ra == rb {
            return None;
        }
        let size_a = self.meta[ra as usize].size;
        let size_b = self.meta[rb as usize].size;

        // Smaller absorbed into larger. If equal, ra survives.
        let (absorbed_root, surviving_root) = if size_a >= size_b {
            (rb, ra)
        } else {
            (ra, rb)
        };

        self.parent[absorbed_root as usize] = surviving_root;
        // Only surviving root needs updated size; absorbed root is no longer
        // a root so its meta doesn't matter.
        self.meta[surviving_root as usize].size =
            self.meta[ra as usize].size + self.meta[rb as usize].size;

        // Rank: only bump when merging equal-size (keeps tree shallow).
        if size_a == size_b {
            self.rank[surviving_root as usize] =
                self.rank[surviving_root as usize].saturating_add(1);
        }

        Some(MergeResult {
            absorbed_root,
            surviving_root,
            surviving_component_id: self.meta[surviving_root as usize].component_id,
            absorbed_component_id: self.meta[absorbed_root as usize].component_id,
        })
    }

    /// Get the component_id for the root of `x`.
    pub fn component_id_of_root(&self, root: NodeIdx) -> ComponentId {
        self.meta[root as usize].component_id
    }

    /// Set the component_id on a root. Used after split detection reassigns.
    pub fn set_component_id_of_root(&mut self, root: NodeIdx, id: ComponentId) {
        self.meta[root as usize].component_id = id;
    }

    /// Size of the component rooted at `root`.
    pub fn size_of_root(&self, root: NodeIdx) -> u32 {
        self.meta[root as usize].size
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

    /// Reset a node to be its own root (used after split detection creates
    /// new partitions via BFS  UF is rebuilt per partition).
    pub fn reset_to_singleton(&mut self, x: NodeIdx, component_id: ComponentId) {
        self.parent[x as usize] = x;
        self.rank[x as usize] = 0;
        self.meta[x as usize] = RootMeta { component_id, size: 1 };
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn three_nodes_union() {
        let mut uf = UnionFind::default();
        let n0 = uf.push_singleton(0);
        let n1 = uf.push_singleton(1);
        let n2 = uf.push_singleton(2);

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
        let n0 = uf.push_singleton(0);
        let n1 = uf.push_singleton(1);
        uf.union(n0, n1);
        let result = uf.union(n0, n1);
        assert!(result.is_none(), "union of already-same component returns None");
    }

    #[test]
    fn path_compression_works() {
        let mut uf = UnionFind::default();
        let n0 = uf.push_singleton(0);
        let n1 = uf.push_singleton(1);
        let n2 = uf.push_singleton(2);
        let n3 = uf.push_singleton(3);
        uf.union(n0, n1);
        uf.union(n1, n2);
        uf.union(n2, n3);
        let root = uf.find(n3);
        assert_eq!(uf.find(n0), root);
        assert_eq!(uf.find(n1), root);
        assert_eq!(uf.find(n2), root);
        assert_eq!(uf.find(n3), root);
    }

    #[test]
    fn root_meta_component_id_and_size_after_union() {
        let mut uf = UnionFind::default();
        let n0 = uf.push_singleton(10); // component_id = 10
        let n1 = uf.push_singleton(20); // component_id = 20

        let merge = uf.union(n0, n1).unwrap();
        let surviving = merge.surviving_root;
        // Surviving size should be 2
        assert_eq!(uf.size_of_root(surviving), 2);
        // Surviving component_id is the one that was on the surviving root
        assert_eq!(
            uf.component_id_of_root(surviving),
            merge.surviving_component_id
        );
    }
}
