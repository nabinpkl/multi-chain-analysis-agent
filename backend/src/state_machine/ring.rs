use std::collections::{HashMap, VecDeque};
use std::sync::Arc;

use crate::state_machine::WalletId;

/// One ingested edge event retained for window eviction.
pub struct TempEntry {
    pub block_time: u32,
    pub from: WalletId,
    pub to: WalletId,
    pub amount: u64,
}

/// Exact temporal ring for 24h window eviction. Holds every edge event
/// with interned `Arc<str>` wallet ids so memory is dominated by the
/// interner's unique string pool, not by the ring length.
pub struct TemporalRing {
    pub buf: VecDeque<TempEntry>,
    interner: HashMap<String, WalletId>,
}

impl TemporalRing {
    pub fn new() -> Self {
        Self {
            buf: VecDeque::new(),
            interner: HashMap::new(),
        }
    }

    pub fn intern(&mut self, s: &str) -> WalletId {
        if let Some(existing) = self.interner.get(s) {
            return existing.clone();
        }
        let arc: Arc<str> = Arc::from(s);
        self.interner.insert(s.to_string(), arc.clone());
        arc
    }

    pub fn push(&mut self, entry: TempEntry) {
        self.buf.push_back(entry);
    }

    pub fn pop_older_than(&mut self, cutoff: u32) -> Option<TempEntry> {
        match self.buf.front() {
            Some(front) if front.block_time < cutoff => self.buf.pop_front(),
            _ => None,
        }
    }

    /// Drop interned strings that are no longer referenced anywhere outside
    /// the interner itself. Called periodically to reclaim memory from
    /// wallets that have fully aged out of the window.
    pub fn gc_interner(&mut self) {
        self.interner.retain(|_, v| Arc::strong_count(v) > 1);
    }

    pub fn len(&self) -> usize {
        self.buf.len()
    }

    pub fn interner_len(&self) -> usize {
        self.interner.len()
    }
}
