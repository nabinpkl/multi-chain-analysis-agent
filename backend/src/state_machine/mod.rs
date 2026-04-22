//! Live graph projection maintained in RAM.
//!
//! The state machine is the hot path for the overview API. It holds the
//! full 24h sliding window of parsed edges as aggregated projections,
//! plus the temporal ring needed to evict them, plus top-K indices over
//! both edge pairs and individual wallets. Reads are O(K + components);
//! writes are O(log n) in the number of distinct pairs/wallets.
//!
//! ## Transitions
//! - `Increment(edge)` — called by the `state_sink` consumer on each
//!   Kafka message. Adds to edge/wallet aggregates, pushes to the ring,
//!   updates running stats. Happens hundreds of times per second.
//! - `AdvanceWindow(now)` — called by a 1Hz tick. Drains ring entries
//!   older than `now - window_secs`, subtracts them from aggregates, and
//!   removes keys whose score dropped to zero.
//!
//! ## Non-idempotent on replay (known limitation)
//!
//! Aggregate updates are additive (`volume += amount`), so a Kafka redelivery
//! after a crash-before-commit double-counts. Acceptable for v0 because the
//! external `state-reset` script wipes state on every restart, so duplicates
//! only accumulate within a single uptime — at most the N-message commit
//! window (1000 msgs / 2s). When moving to paid RPC and dropping the reset
//! script, add LRU dedupe on `(signature, instruction_idx)` in `apply`.

use std::collections::HashMap;
use std::sync::Arc;

use crate::domain::{
    Edge, EdgeView, LAMPORTS_PER_SOL, NodeView, OverviewResponse, StatsView, WindowView,
};

pub mod aggregates;
pub mod components;
pub mod delta;
pub mod ring;
pub mod topk;

use aggregates::{EdgeAgg, RunningStats, WalletAgg};
use ring::{TempEntry, TemporalRing};
use topk::TopK;

pub type WalletId = Arc<str>;
pub type EdgeKey = (WalletId, WalletId);

const RECENT_RATE_WINDOW_SECS: u32 = 30;

pub enum Transition {
    Increment(Edge),
    AdvanceWindow(u32),
}

pub struct StateMachine {
    window_secs: u32,
    top_edges_cap: usize,
    whale_pad: usize,
    edge_agg: HashMap<EdgeKey, EdgeAgg>,
    edge_topk: TopK<EdgeKey>,
    wallet_agg: HashMap<WalletId, WalletAgg>,
    wallet_topk: TopK<WalletId>,
    stats: RunningStats,
    ring: TemporalRing,
    latest_block_time: u32,
    stream_start_ts: Option<u32>,
    seq: u64,
}

impl StateMachine {
    pub fn new(window_secs: u32, top_edges_cap: usize, whale_pad: usize) -> Self {
        Self {
            window_secs,
            top_edges_cap,
            whale_pad,
            edge_agg: HashMap::new(),
            edge_topk: TopK::default(),
            wallet_agg: HashMap::new(),
            wallet_topk: TopK::default(),
            stats: RunningStats::default(),
            ring: TemporalRing::new(),
            latest_block_time: 0,
            stream_start_ts: None,
            seq: 0,
        }
    }

    pub fn apply(&mut self, t: Transition) {
        match t {
            Transition::Increment(edge) => self.increment(edge),
            Transition::AdvanceWindow(now) => self.advance_window(now),
        }
    }

    fn increment(&mut self, edge: Edge) {
        let from = self.ring.intern(&edge.from_wallet);
        let to = self.ring.intern(&edge.to_wallet);
        let key = (from.clone(), to.clone());

        // Edge aggregate
        let ea = self.edge_agg.entry(key.clone()).or_default();
        ea.volume += edge.amount;
        ea.tx_count += 1;
        let new_edge_score = ea.volume;
        self.edge_topk.upsert(key.clone(), new_edge_score);

        // Wallet aggregates
        let wa_from = self.wallet_agg.entry(from.clone()).or_default();
        wa_from.volume += edge.amount;
        let from_score = wa_from.volume;
        self.wallet_topk.upsert(from.clone(), from_score);

        let wa_to = self.wallet_agg.entry(to.clone()).or_default();
        wa_to.volume += edge.amount;
        let to_score = wa_to.volume;
        self.wallet_topk.upsert(to.clone(), to_score);

        // Stats
        self.stats.total_volume += edge.amount;
        self.stats.total_txs += 1;
        self.stats.unique_wallets.insert(from.clone());
        self.stats.unique_wallets.insert(to.clone());

        // Ring for eviction
        self.ring.push(TempEntry {
            block_time: edge.block_time,
            from,
            to,
            amount: edge.amount,
        });

        if edge.block_time > self.latest_block_time {
            self.latest_block_time = edge.block_time;
        }
        if self.stream_start_ts.is_none() {
            self.stream_start_ts = Some(edge.block_time);
        }

        self.seq += 1;
    }

    fn advance_window(&mut self, now: u32) {
        // Use max(host_now, latest_block_time) — self-correcting against
        // host clock skew. During catch-up this trails Solana's clock.
        let reference = now.max(self.latest_block_time);
        let cutoff = reference.saturating_sub(self.window_secs);

        let mut drained: u64 = 0;
        while let Some(entry) = self.ring.pop_older_than(cutoff) {
            drained += 1;

            // Subtract from edge agg
            let key = (entry.from.clone(), entry.to.clone());
            if let Some(ea) = self.edge_agg.get_mut(&key) {
                ea.volume = ea.volume.saturating_sub(entry.amount);
                ea.tx_count = ea.tx_count.saturating_sub(1);
                let new_score = ea.volume;
                if new_score == 0 {
                    self.edge_agg.remove(&key);
                    self.edge_topk.remove(&key);
                } else {
                    self.edge_topk.upsert(key, new_score);
                }
            }

            // Subtract from wallet aggs
            if let Some(wa) = self.wallet_agg.get_mut(&entry.from) {
                wa.volume = wa.volume.saturating_sub(entry.amount);
                let new_score = wa.volume;
                if new_score == 0 {
                    self.wallet_agg.remove(&entry.from);
                    self.wallet_topk.remove(&entry.from);
                    self.stats.unique_wallets.remove(&entry.from);
                } else {
                    self.wallet_topk.upsert(entry.from.clone(), new_score);
                }
            }
            if let Some(wa) = self.wallet_agg.get_mut(&entry.to) {
                wa.volume = wa.volume.saturating_sub(entry.amount);
                let new_score = wa.volume;
                if new_score == 0 {
                    self.wallet_agg.remove(&entry.to);
                    self.wallet_topk.remove(&entry.to);
                    self.stats.unique_wallets.remove(&entry.to);
                } else {
                    self.wallet_topk.upsert(entry.to.clone(), new_score);
                }
            }

            // Stats
            self.stats.total_volume = self.stats.total_volume.saturating_sub(entry.amount);
            self.stats.total_txs = self.stats.total_txs.saturating_sub(1);

            // Advance stream_start_ts as we drop old data
            if let Some(next_front) = self.ring.buf.front() {
                self.stream_start_ts = Some(next_front.block_time);
            }
        }

        if drained > 0 {
            // Periodically reclaim interner memory; cheap at ring sizes we
            // actually see because the HashMap just walks its own entries.
            self.ring.gc_interner();
        }

        self.seq += 1;
    }

    /// Snapshot for a requested sub-window of the maintained state.
    ///
    /// Two paths:
    /// - If the requested window is ≥ the maintained window, fall through
    ///   to `snapshot()` which uses the always-maintained top-K indices.
    /// - Otherwise, scan the ring backward from the newest entry until we
    ///   cross the cutoff, accumulating fresh aggregates on the fly. Cost
    ///   is O(entries-in-window) which at ~250 edges/sec stays tractable
    ///   even for 6h (~5M entries → tens of ms).
    pub fn snapshot_window(&self, now: u32, window_secs: u32) -> OverviewResponse {
        if window_secs >= self.window_secs {
            return self.snapshot(now);
        }

        let reference = now.max(self.latest_block_time);
        let cutoff = reference.saturating_sub(window_secs);

        let mut edge_agg: HashMap<EdgeKey, EdgeAgg> = HashMap::new();
        let mut wallet_agg: HashMap<WalletId, WalletAgg> = HashMap::new();
        let mut unique_wallets: std::collections::HashSet<WalletId> =
            std::collections::HashSet::new();
        let mut total_volume: u64 = 0;
        let mut total_txs: u64 = 0;
        let mut earliest_observed: u32 = u32::MAX;

        for entry in self.ring.buf.iter().rev() {
            if entry.block_time < cutoff {
                break;
            }
            let key = (entry.from.clone(), entry.to.clone());
            let ea = edge_agg.entry(key).or_default();
            ea.volume += entry.amount;
            ea.tx_count += 1;

            let wa_from = wallet_agg.entry(entry.from.clone()).or_default();
            wa_from.volume += entry.amount;
            let wa_to = wallet_agg.entry(entry.to.clone()).or_default();
            wa_to.volume += entry.amount;

            unique_wallets.insert(entry.from.clone());
            unique_wallets.insert(entry.to.clone());

            total_volume += entry.amount;
            total_txs += 1;

            if entry.block_time < earliest_observed {
                earliest_observed = entry.block_time;
            }
        }

        // Build top-K edges by volume desc.
        let mut top_edges_vec: Vec<(EdgeKey, u64, u64)> = edge_agg
            .iter()
            .map(|(k, v)| (k.clone(), v.volume, v.tx_count))
            .collect();
        top_edges_vec.sort_unstable_by(|a, b| b.1.cmp(&a.1));
        top_edges_vec.truncate(self.top_edges_cap);

        // Build wallet ranking by volume desc.
        let mut top_wallets_vec: Vec<(WalletId, u64)> = wallet_agg
            .iter()
            .map(|(k, v)| (k.clone(), v.volume))
            .collect();
        top_wallets_vec.sort_unstable_by(|a, b| b.1.cmp(&a.1));

        // Nodes from top-K edge endpoints.
        let mut node_set: std::collections::HashSet<WalletId> =
            std::collections::HashSet::new();
        let mut edge_pairs: Vec<(&str, &str)> = Vec::with_capacity(top_edges_vec.len());
        for ((from, to), _, _) in &top_edges_vec {
            node_set.insert(from.clone());
            node_set.insert(to.clone());
            edge_pairs.push((from.as_ref(), to.as_ref()));
        }

        let cc = components::connected_components(&edge_pairs);
        let cc_owned: HashMap<String, u32> =
            cc.into_iter().map(|(k, v)| (k.to_string(), v)).collect();

        let edges: Vec<EdgeView> = top_edges_vec
            .iter()
            .map(|((from, to), vol, tx_count)| EdgeView {
                from: from.to_string(),
                to: to.to_string(),
                volume_sol: lamports_to_sol(*vol),
                tx_count: *tx_count,
            })
            .collect();

        let mut nodes: Vec<NodeView> =
            Vec::with_capacity(node_set.len() + self.whale_pad);
        for n in &node_set {
            let volume = wallet_agg.get(n).map(|w| w.volume).unwrap_or(0);
            nodes.push(NodeView {
                id: n.to_string(),
                volume_sol: lamports_to_sol(volume),
                component: cc_owned.get(n.as_ref()).copied(),
            });
        }

        let mut added = 0usize;
        for (wallet, score) in &top_wallets_vec {
            if added >= self.whale_pad {
                break;
            }
            if node_set.contains(wallet) {
                continue;
            }
            nodes.push(NodeView {
                id: wallet.to_string(),
                volume_sol: lamports_to_sol(*score),
                component: None,
            });
            added += 1;
        }

        let (top_wallet, top_wallet_volume_sol) = top_wallets_vec
            .first()
            .map(|(w, v)| (Some(w.to_string()), Some(lamports_to_sol(*v))))
            .unwrap_or((None, None));

        let stats = StatsView {
            total_volume_sol: lamports_to_sol(total_volume),
            total_txs,
            unique_wallets: unique_wallets.len() as u64,
            top_wallet,
            top_wallet_volume_sol,
            tx_per_sec_recent: self.recent_tx_rate(RECENT_RATE_WINDOW_SECS),
        };

        let effective_from = if total_txs > 0 {
            earliest_observed.max(cutoff)
        } else {
            reference
        };
        let elapsed = reference.saturating_sub(effective_from);
        let is_partial = elapsed < window_secs;

        OverviewResponse {
            window: WindowView {
                from: effective_from,
                to: reference,
                label: humanize_duration(elapsed),
            },
            stats,
            nodes,
            edges,
            generated_at: reference,
            is_partial,
        }
    }

    /// Expose a snapshot of current projection — collected under the caller's
    /// read lock, serialized afterwards by the API layer.
    pub fn snapshot(&self, now: u32) -> OverviewResponse {
        let reference = now.max(self.latest_block_time);
        let effective_from = self
            .stream_start_ts
            .map(|t| t.max(reference.saturating_sub(self.window_secs)))
            .unwrap_or(reference);

        // Collect top-500 edges
        let top_edges: Vec<(EdgeKey, u64)> = self
            .edge_topk
            .top_n(self.top_edges_cap)
            .map(|(k, v)| (k.clone(), v))
            .collect();

        // Build node set from endpoints
        let mut node_set: std::collections::HashSet<WalletId> = std::collections::HashSet::new();
        let mut edge_pairs: Vec<(&str, &str)> = Vec::with_capacity(top_edges.len());
        for ((from, to), _) in &top_edges {
            node_set.insert(from.clone());
            node_set.insert(to.clone());
            edge_pairs.push((from.as_ref(), to.as_ref()));
        }

        // CC over the top-500 edges
        let cc = components::connected_components(&edge_pairs);
        let cc_owned: HashMap<String, u32> =
            cc.into_iter().map(|(k, v)| (k.to_string(), v)).collect();

        // Build edge views
        let edges: Vec<EdgeView> = top_edges
            .iter()
            .map(|((from, to), _)| {
                let agg = self.edge_agg.get(&(from.clone(), to.clone()));
                EdgeView {
                    from: from.to_string(),
                    to: to.to_string(),
                    volume_sol: lamports_to_sol(agg.map(|a| a.volume).unwrap_or(0)),
                    tx_count: agg.map(|a| a.tx_count).unwrap_or(0),
                }
            })
            .collect();

        // Build node views — endpoints first
        let mut nodes: Vec<NodeView> = Vec::with_capacity(node_set.len() + self.whale_pad);
        for n in &node_set {
            let volume = self.wallet_agg.get(n).map(|w| w.volume).unwrap_or(0);
            nodes.push(NodeView {
                id: n.to_string(),
                volume_sol: lamports_to_sol(volume),
                component: cc_owned.get(n.as_ref()).copied(),
            });
        }

        // Whale pad: top wallets not already rendered as endpoints.
        let mut added = 0usize;
        for (wallet, score) in self.wallet_topk.top_n(self.whale_pad + node_set.len()) {
            if added >= self.whale_pad {
                break;
            }
            if node_set.contains(wallet) {
                continue;
            }
            nodes.push(NodeView {
                id: wallet.to_string(),
                volume_sol: lamports_to_sol(score),
                component: None,
            });
            added += 1;
        }

        // Stats
        let top_wallet_view = self
            .wallet_topk
            .top_n(1)
            .next()
            .map(|(w, v)| (w.to_string(), lamports_to_sol(v)));
        let (top_wallet, top_wallet_volume_sol) = match top_wallet_view {
            Some((w, v)) => (Some(w), Some(v)),
            None => (None, None),
        };

        let stats = StatsView {
            total_volume_sol: lamports_to_sol(self.stats.total_volume),
            total_txs: self.stats.total_txs,
            unique_wallets: self.stats.unique_wallets.len() as u64,
            top_wallet,
            top_wallet_volume_sol,
            tx_per_sec_recent: self.recent_tx_rate(RECENT_RATE_WINDOW_SECS),
        };

        let elapsed = reference.saturating_sub(effective_from);
        let is_partial = elapsed < self.window_secs;
        let effective_label = humanize_duration(elapsed);

        OverviewResponse {
            window: WindowView {
                from: effective_from,
                to: reference,
                label: effective_label,
            },
            stats,
            nodes,
            edges,
            generated_at: reference,
            is_partial,
        }
    }

    /// Tx rate over the trailing `window_secs` of ring data, anchored to
     /// the newest ring entry's block_time (not wall-clock `now`). This
     /// is what keeps the pill meaningful during catch-up, when the
     /// ingester lags tip and host_now is far ahead of the newest data.
     /// At tip, newest_block_time ≈ host_now, so the two agree.
    fn recent_tx_rate(&self, window_secs: u32) -> f64 {
        if window_secs == 0 {
            return 0.0;
        }
        let newest = match self.ring.buf.back() {
            Some(e) => e.block_time,
            None => return 0.0,
        };
        let cutoff = newest.saturating_sub(window_secs);
        let mut count: u64 = 0;
        let mut oldest: Option<u32> = None;
        for entry in self.ring.buf.iter().rev() {
            if entry.block_time < cutoff {
                break;
            }
            count += 1;
            oldest = Some(entry.block_time);
        }
        let span = match oldest {
            Some(t) => newest.saturating_sub(t).max(1) as f64,
            None => return 0.0,
        };
        count as f64 / span
    }

    pub fn seq(&self) -> u64 {
        self.seq
    }

    pub fn stream_start_ts(&self) -> Option<u32> {
        self.stream_start_ts
    }

    pub fn latest_block_time(&self) -> u32 {
        self.latest_block_time
    }

    pub fn edge_agg_len(&self) -> usize {
        self.edge_agg.len()
    }

    pub fn wallet_agg_len(&self) -> usize {
        self.wallet_agg.len()
    }

    pub fn ring_len(&self) -> usize {
        self.ring.len()
    }
}

fn lamports_to_sol(lamports: u64) -> f64 {
    lamports as f64 / LAMPORTS_PER_SOL
}

fn humanize_duration(secs: u32) -> String {
    if secs < 60 {
        format!("{}s", secs)
    } else if secs < 3600 {
        format!("{}m", secs / 60)
    } else if secs < 86400 {
        format!("{}h {}m", secs / 3600, (secs % 3600) / 60)
    } else {
        format!("{}d {}h", secs / 86400, (secs % 86400) / 3600)
    }
}
