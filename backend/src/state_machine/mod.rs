//! Live graph projection maintained in RAM.
//!
//! The state machine is the hot path for the overview API. It holds the
//! full window of parsed edges as aggregated projections, plus the
//! temporal ring needed to evict them. Reads build a hub-view subgraph
//! on demand; writes are O(log n) in the number of distinct wallets (for
//! the wallet volume index used to name the top wallet).
//!
//! ## Transitions
//! - `Increment(edge)`  called by the `state_sink` consumer on each
//!   Kafka message. Adds to edge/wallet aggregates, pushes to the ring,
//!   updates running stats. Happens hundreds of times per second.
//! - `AdvanceWindow(now)`  called by a 1Hz tick. Drains ring entries
//!   older than `now - window_secs`, subtracts them from aggregates, and
//!   removes keys whose score dropped to zero.
//!
//! ## Hub view (graph lens)
//!
//! Snapshots render a *hub subgraph*: the top `HUB_COUNT` wallets by
//! degree (distinct counterparties, volume-tiebroken) and every edge that
//! touches one of them, capped at `EDGE_CAP` by volume. This reads as a
//! constellation  exchanges as stars, drainers as sudden fanouts,
//! bridges as new hub-to-hub links  instead of a volume-ranked event
//! list. The cost of the subgraph build is O(|edge_agg|) per snapshot,
//! which is cheap at ≤ 2 Hz snapshot rate.
//!
//! ## Non-idempotent on replay (known limitation)
//!
//! Aggregate updates are additive (`volume += amount`), so a Kafka redelivery
//! after a crash-before-commit double-counts. Acceptable for v0 because the
//! external `state-reset` script wipes state on every restart, so duplicates
//! only accumulate within a single uptime  at most the N-message commit
//! window (1000 msgs / 2s). When moving to paid RPC and dropping the reset
//! script, add LRU dedupe on `(signature, instruction_idx)` in `apply`.

use std::collections::{HashMap, HashSet};
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
const HUB_COUNT: usize = 50;
/// Fraction of total hub-touching volume we try to preserve when
/// selecting which edges to render. Principled Pareto filter  keep
/// sorting by volume desc until we've covered this much, then stop.
/// Power-law edge distributions mean this typically lands in the
/// top ~10-20% of edges while preserving every structurally heavy
/// corridor.
const VOLUME_COVERAGE: f64 = 0.80;

pub enum Transition {
    Increment(Edge),
    AdvanceWindow(u32),
}

pub struct StateMachine {
    window_secs: u32,
    edge_agg: HashMap<EdgeKey, EdgeAgg>,
    wallet_agg: HashMap<WalletId, WalletAgg>,
    wallet_topk: TopK<WalletId>,
    stats: RunningStats,
    ring: TemporalRing,
    latest_block_time: u32,
    stream_start_ts: Option<u32>,
    seq: u64,
}

impl StateMachine {
    pub fn new(window_secs: u32) -> Self {
        Self {
            window_secs,
            edge_agg: HashMap::new(),
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
        let ea = self.edge_agg.entry(key).or_default();
        ea.volume += edge.amount;
        ea.tx_count += 1;

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
        // Use max(host_now, latest_block_time)  self-correcting against
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
                if ea.volume == 0 {
                    self.edge_agg.remove(&key);
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

    /// Snapshot for a requested sub-window of the maintained state.
    ///
    /// Two paths:
    /// - If the requested window is ≥ the maintained window, fall through
    ///   to `snapshot()` which uses the live aggregate maps.
    /// - Otherwise, scan the ring backward from the newest entry until we
    ///   cross the cutoff, accumulating temp aggregates on the fly, then
    ///   feed those into `build_hub_subgraph`.
    pub fn snapshot_window(&self, now: u32, window_secs: u32) -> OverviewResponse {
        if window_secs >= self.window_secs {
            return self.snapshot(now);
        }

        let reference = now.max(self.latest_block_time);
        let cutoff = reference.saturating_sub(window_secs);

        let mut edge_agg: HashMap<EdgeKey, EdgeAgg> = HashMap::new();
        let mut wallet_agg: HashMap<WalletId, WalletAgg> = HashMap::new();
        let mut unique_wallets: HashSet<WalletId> = HashSet::new();
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

        let (nodes, edges) = build_hub_subgraph(&edge_agg, &wallet_agg);

        // Top wallet over the temp wallet_agg.
        let (top_wallet, top_wallet_volume_sol) = wallet_agg
            .iter()
            .max_by_key(|(_, v)| v.volume)
            .map(|(w, v)| (Some(w.to_string()), Some(lamports_to_sol(v.volume))))
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

    /// Snapshot over the full maintained window. Uses the live aggregate
    /// maps directly.
    pub fn snapshot(&self, now: u32) -> OverviewResponse {
        let reference = now.max(self.latest_block_time);
        let effective_from = self
            .stream_start_ts
            .map(|t| t.max(reference.saturating_sub(self.window_secs)))
            .unwrap_or(reference);

        let (nodes, edges) = build_hub_subgraph(&self.edge_agg, &self.wallet_agg);

        // Top wallet via the maintained volume-ranked index.
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

/// Build the hub-view subgraph from edge/wallet aggregate maps.
///
/// Algorithm:
///   1. Derive each wallet's degree = |distinct counterparties| by walking
///      edge_agg keys once.
///   2. Rank wallets by (degree desc, volume desc) and take top HUB_COUNT.
///   3. Keep every edge_agg entry that touches any hub. No global cap 
///      the hub definition itself bounds output: |hubs| * max_hub_degree
///      is the worst case, which in a power-law graph is tame.
///   4. Node set = endpoints of the kept edges (hubs + their 1-hop orbits).
///   5. Run connected_components over the kept edges for stable coloring.
///
/// Cost: O(|edge_agg| + |wallets|). At ≤ 2 Hz snapshot rate this stays
/// cheap even at 50k live edges.
fn build_hub_subgraph(
    edge_agg: &HashMap<EdgeKey, EdgeAgg>,
    wallet_agg: &HashMap<WalletId, WalletAgg>,
) -> (Vec<NodeView>, Vec<EdgeView>) {
    // 1. Distinct counterparties per wallet.
    let mut counterparties: HashMap<WalletId, HashSet<WalletId>> = HashMap::new();
    for (from, to) in edge_agg.keys() {
        counterparties
            .entry(from.clone())
            .or_default()
            .insert(to.clone());
        counterparties
            .entry(to.clone())
            .or_default()
            .insert(from.clone());
    }

    // 2. Rank by (degree desc, volume desc).
    let mut ranked: Vec<(WalletId, usize, u64)> = counterparties
        .iter()
        .map(|(w, cps)| {
            let vol = wallet_agg.get(w).map(|wa| wa.volume).unwrap_or(0);
            (w.clone(), cps.len(), vol)
        })
        .collect();
    ranked.sort_unstable_by(|a, b| b.1.cmp(&a.1).then_with(|| b.2.cmp(&a.2)));

    // 3. Top HUB_COUNT as the hub set.
    let hubs: HashSet<WalletId> = ranked
        .iter()
        .take(HUB_COUNT)
        .map(|(w, _, _)| w.clone())
        .collect();

    // 4. Edges touching any hub. Apply a volume-coverage filter: sort
    // by volume desc and keep just enough edges to capture
    // VOLUME_COVERAGE of total hub-touching volume. No magic K  the
    // truncation point is derived from the data.
    let mut included: Vec<(WalletId, WalletId, u64, u64)> = edge_agg
        .iter()
        .filter(|((from, to), _)| hubs.contains(from) || hubs.contains(to))
        .map(|((from, to), agg)| (from.clone(), to.clone(), agg.volume, agg.tx_count))
        .collect();
    included.sort_unstable_by(|a, b| b.2.cmp(&a.2));
    let total_volume: u128 = included.iter().map(|(_, _, v, _)| *v as u128).sum();
    if total_volume > 0 {
        let target = (total_volume as f64 * VOLUME_COVERAGE) as u128;
        let mut accum: u128 = 0;
        let mut keep = 0;
        for (i, (_, _, v, _)) in included.iter().enumerate() {
            accum += *v as u128;
            keep = i + 1;
            if accum >= target {
                break;
            }
        }
        included.truncate(keep);
    }

    // 5. Node set from endpoints + edge pairs for CC input.
    let mut node_set: HashSet<WalletId> = HashSet::new();
    let mut edge_pairs: Vec<(&str, &str)> = Vec::with_capacity(included.len());
    for (from, to, _, _) in &included {
        node_set.insert(from.clone());
        node_set.insert(to.clone());
        edge_pairs.push((from.as_ref(), to.as_ref()));
    }

    let cc = components::connected_components(&edge_pairs);
    let cc_owned: HashMap<String, u32> =
        cc.into_iter().map(|(k, v)| (k.to_string(), v)).collect();

    let edges: Vec<EdgeView> = included
        .iter()
        .map(|(from, to, vol, tx)| EdgeView {
            from: from.to_string(),
            to: to.to_string(),
            volume_sol: lamports_to_sol(*vol),
            tx_count: *tx,
        })
        .collect();

    let mut nodes: Vec<NodeView> = node_set
        .iter()
        .map(|w| {
            let volume = wallet_agg.get(w).map(|wa| wa.volume).unwrap_or(0);
            let degree = counterparties.get(w).map(|cps| cps.len()).unwrap_or(0) as u32;
            NodeView {
                id: w.to_string(),
                volume_sol: lamports_to_sol(volume),
                component: cc_owned.get(w.as_ref()).copied(),
                degree,
                x: 0.0,
                y: 0.0,
            }
        })
        .collect();

    // Sort hubs-first so the frontend can iterate in order. Positions
    // are stamped later in the API handler from the force-sim store.
    nodes.sort_unstable_by(|a, b| b.degree.cmp(&a.degree));

    (nodes, edges)
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
