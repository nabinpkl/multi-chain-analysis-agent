use std::collections::HashMap;
use std::future::Future;
use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::sync::Mutex;

use crate::domain::OverviewResponse;

#[derive(Debug, Clone, Hash, Eq, PartialEq)]
pub struct CacheKey {
    pub window_label: &'static str,
    pub edge_limit: u32,
    pub whale_pad: u32,
}

pub struct OverviewCache {
    ttl: Duration,
    state: Mutex<HashMap<CacheKey, (Instant, Arc<OverviewResponse>)>>,
}

impl OverviewCache {
    pub fn new(ttl: Duration) -> Self {
        Self {
            ttl,
            state: Mutex::new(HashMap::new()),
        }
    }

    pub fn ttl_secs(&self) -> u32 {
        self.ttl.as_secs() as u32
    }

    pub async fn get_or_compute<F, Fut>(
        &self,
        key: CacheKey,
        compute: F,
    ) -> anyhow::Result<Arc<OverviewResponse>>
    where
        F: FnOnce() -> Fut,
        Fut: Future<Output = anyhow::Result<OverviewResponse>>,
    {
        let mut guard = self.state.lock().await;
        if let Some((at, resp)) = guard.get(&key)
            && at.elapsed() < self.ttl
        {
            return Ok(resp.clone());
        }
        let resp = compute().await?;
        let arc = Arc::new(resp);
        guard.insert(key, (Instant::now(), arc.clone()));
        Ok(arc)
    }
}
