//! Single-flight (request coalescing) for `async` work keyed by `K`.
//!
//! When N concurrent callers invoke `run(key, work)` with the same
//! key, only one of them executes `work`; the rest await the leader's
//! result. As soon as the leader's future completes, the entry is
//! evicted so the next non-overlapping call starts a fresh flight.
//! There is no caching: this is purely an in-flight-window optimizer.
//!
//! Today's only consumer is `metadata::fetch::fetch_token_metadata`
//! (key = mint base58 string), where two concurrent agent calls for
//! the same mint should coalesce into one `getAccountInfo` pair
//! against mainnet. The shape is generic so future RPC consumers
//! that want per-key dedup can reuse it without a refactor.
//!
//! ## Implementation notes
//!
//! - The map is `DashMap<K, Arc<tokio::sync::OnceCell<V>>>`. The Arc
//!   over the cell lets followers retain the cell after the leader
//!   has evicted the dashmap entry; the cell stays alive as long as
//!   any task holds an Arc into it.
//! - `tokio::sync::OnceCell::get_or_init` does NOT poison on panic:
//!   if the leader's future panics, followers are not stranded.
//! - The DashMap entry guard returned by `entry().or_insert_with(...)`
//!   holds a write lock on the shard. Holding it across `await` would
//!   deadlock anyone else touching the same shard. We extract the
//!   inner `Arc<OnceCell>` via `.clone()` and drop the guard before
//!   any await  see comments in `run`.

use std::future::Future;
use std::hash::Hash;
use std::sync::Arc;

use dashmap::DashMap;
use tokio::sync::OnceCell;

pub struct Singleflight<K, V>
where
    K: Eq + Hash + Clone + Send + Sync + 'static,
    V: Clone + Send + Sync + 'static,
{
    inflight: DashMap<K, Arc<OnceCell<V>>>,
}

impl<K, V> Default for Singleflight<K, V>
where
    K: Eq + Hash + Clone + Send + Sync + 'static,
    V: Clone + Send + Sync + 'static,
{
    fn default() -> Self {
        Self {
            inflight: DashMap::new(),
        }
    }
}

impl<K, V> Singleflight<K, V>
where
    K: Eq + Hash + Clone + Send + Sync + 'static,
    V: Clone + Send + Sync + 'static,
{
    pub fn new() -> Self {
        Self::default()
    }

    /// Run `f` under `key`. Concurrent callers with the same key
    /// share a single execution: the first caller to insert the cell
    /// is the leader and runs `f`; followers await the leader's
    /// result. Once the leader's future completes, the entry is
    /// removed so the next non-overlapping call starts fresh.
    ///
    /// Returns a clone of the value the leader produced. Cheap when
    /// `V` is itself reference-counted (e.g. `Arc<...>`); for value
    /// types like `Result<Option<OnChainMetadata>, RpcError>` the
    /// clone is a shallow copy of the small struct + the small enum.
    pub async fn run<F, Fut>(&self, key: K, f: F) -> V
    where
        F: FnOnce() -> Fut,
        Fut: Future<Output = V>,
    {
        // Acquire (or create) the cell for this key. The block scope
        // ensures the dashmap entry guard drops BEFORE the await
        // below; holding the shard write lock across `await` would
        // deadlock any concurrent call touching the same shard.
        let cell: Arc<OnceCell<V>> = {
            let entry = self
                .inflight
                .entry(key.clone())
                .or_insert_with(|| Arc::new(OnceCell::new()));
            Arc::clone(entry.value())
        };

        // Leader runs `f` once; followers await the same cell. The
        // returned `&V` is cloned out so callers own the value.
        let value = cell.get_or_init(f).await.clone();

        // Evict so the next non-overlapping call starts a fresh
        // flight. Followers that already hold the Arc<OnceCell> keep
        // reading their initialized cell; the Arc keeps it alive
        // beyond the dashmap remove.
        self.inflight.remove(&key);

        value
    }

    #[cfg(test)]
    pub fn inflight_len(&self) -> usize {
        self.inflight.len()
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use std::time::Duration;

    use super::Singleflight;

    /// 20 concurrent callers with the same key collapse into one
    /// closure execution; all 20 receive the same value.
    #[tokio::test]
    async fn dedupes_concurrent_calls_same_key() {
        let sf: Arc<Singleflight<String, u32>> = Arc::new(Singleflight::new());
        let counter = Arc::new(AtomicUsize::new(0));

        let mut handles = Vec::new();
        for _ in 0..20 {
            let sf = Arc::clone(&sf);
            let counter = Arc::clone(&counter);
            handles.push(tokio::spawn(async move {
                sf.run("k".to_string(), || async move {
                    counter.fetch_add(1, Ordering::SeqCst);
                    // Hold the leader's flight long enough for the
                    // 19 followers to enqueue behind it.
                    tokio::time::sleep(Duration::from_millis(50)).await;
                    42u32
                })
                .await
            }));
        }

        for h in handles {
            assert_eq!(h.await.unwrap(), 42);
        }
        assert_eq!(counter.load(Ordering::SeqCst), 1);
    }

    /// Sequential calls (no overlap) trigger separate executions.
    /// This is the "no caching" property: once the leader finishes
    /// and the entry is evicted, the next call starts fresh.
    #[tokio::test]
    async fn sequential_calls_run_closure_each_time() {
        let sf: Singleflight<String, u32> = Singleflight::new();
        let counter = Arc::new(AtomicUsize::new(0));

        for _ in 0..3 {
            let counter_inner = Arc::clone(&counter);
            let v = sf
                .run("k".to_string(), || async move {
                    counter_inner.fetch_add(1, Ordering::SeqCst);
                    7u32
                })
                .await;
            assert_eq!(v, 7);
        }
        assert_eq!(counter.load(Ordering::SeqCst), 3);
        assert_eq!(sf.inflight_len(), 0);
    }

    /// Different keys do not coalesce; each fires its own closure.
    #[tokio::test]
    async fn different_keys_do_not_dedupe() {
        let sf: Arc<Singleflight<String, u32>> = Arc::new(Singleflight::new());
        let counter = Arc::new(AtomicUsize::new(0));

        let h_a = {
            let sf = Arc::clone(&sf);
            let counter = Arc::clone(&counter);
            tokio::spawn(async move {
                sf.run("a".to_string(), || async move {
                    counter.fetch_add(1, Ordering::SeqCst);
                    tokio::time::sleep(Duration::from_millis(30)).await;
                    1u32
                })
                .await
            })
        };
        let h_b = {
            let sf = Arc::clone(&sf);
            let counter = Arc::clone(&counter);
            tokio::spawn(async move {
                sf.run("b".to_string(), || async move {
                    counter.fetch_add(1, Ordering::SeqCst);
                    tokio::time::sleep(Duration::from_millis(30)).await;
                    2u32
                })
                .await
            })
        };

        assert_eq!(h_a.await.unwrap(), 1);
        assert_eq!(h_b.await.unwrap(), 2);
        assert_eq!(counter.load(Ordering::SeqCst), 2);
    }

    /// Result-typed values with non-Clone errors must be wrapped
    /// before storing here. This test pins that the struct is happy
    /// with `Result<Option<u32>, ()>` (a Clone error stand-in for the
    /// real `RpcError` which now derives Clone).
    #[tokio::test]
    async fn supports_result_value_with_clone_error() {
        let sf: Arc<Singleflight<String, Result<Option<u32>, &'static str>>> =
            Arc::new(Singleflight::new());

        let v_ok = sf
            .run("k1".to_string(), || async { Ok(Some(99u32)) })
            .await;
        assert_eq!(v_ok, Ok(Some(99u32)));

        let v_err = sf
            .run("k2".to_string(), || async {
                Err::<Option<u32>, &'static str>("boom")
            })
            .await;
        assert_eq!(v_err, Err("boom"));
    }
}
