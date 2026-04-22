use std::collections::{BTreeSet, HashMap};
use std::hash::Hash;

/// Top-K index over all active keys, ordered by score.
///
/// Holds the full universe of (key, score) pairs — the `top_n()` read is
/// where the cap is applied. This is necessary because a key not currently
/// in the top N can enter it with a single score bump, and we need O(log n)
/// upsert to re-rank it.
pub struct TopK<K: Eq + Hash + Clone + Ord> {
    by_score: BTreeSet<(u64, K)>,
    by_key: HashMap<K, u64>,
}

impl<K: Eq + Hash + Clone + Ord> Default for TopK<K> {
    fn default() -> Self {
        Self {
            by_score: BTreeSet::new(),
            by_key: HashMap::new(),
        }
    }
}

impl<K: Eq + Hash + Clone + Ord> TopK<K> {
    pub fn upsert(&mut self, key: K, new_score: u64) {
        if let Some(old) = self.by_key.get(&key).copied() {
            if old == new_score {
                return;
            }
            self.by_score.remove(&(old, key.clone()));
        }
        if new_score == 0 {
            self.by_key.remove(&key);
        } else {
            self.by_score.insert((new_score, key.clone()));
            self.by_key.insert(key, new_score);
        }
    }

    pub fn remove(&mut self, key: &K) {
        if let Some(score) = self.by_key.remove(key) {
            self.by_score.remove(&(score, key.clone()));
        }
    }

    /// Iterator over top-N (key, score) in descending score order.
    pub fn top_n(&self, n: usize) -> impl Iterator<Item = (&K, u64)> {
        self.by_score
            .iter()
            .rev()
            .take(n)
            .map(|(score, key)| (key, *score))
    }

    pub fn len(&self) -> usize {
        self.by_key.len()
    }
}
