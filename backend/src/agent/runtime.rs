//! Agent runtime task. v0 logs readiness and idles; ship 1 lands the
//! actual ReAct loop, prompt assembly, output policy gate, and ledger
//! writes. The spawn pattern mirrors `analytics::spawn_all`: clone
//! `AppState`, hold a `watch::Receiver<bool>` shutdown signal, exit
//! cleanly on shutdown.

use tokio::sync::watch;
use tokio::task::JoinHandle;
use tracing::{info, warn};

use super::client::AgentClient;
use super::config::AgentConfig;
use crate::state::AppState;

/// Public face of the agent runtime. v0 is a thin handle that owns
/// the spawn function. Ship 1 grows real methods on the same struct.
pub struct Agent;

impl Agent {
    /// Spawn the runtime task. The task owns no graph or LLM state of
    /// its own beyond `AppState` clones; primitives reach into
    /// `state.graph` under brief read locks (per the analytics-task
    /// pattern).
    pub fn spawn(_state: AppState, mut shutdown: watch::Receiver<bool>) -> JoinHandle<()> {
        tokio::spawn(async move {
            // v0 task is idle; the SSE handler currently does the
            // round-trip directly. When the loop lands in ship 1 the
            // body here owns the per-session work.
            info!("agent runtime online (ship-0 idle task)");
            let _ = shutdown.changed().await;
            warn!("agent runtime shutdown");
        })
    }
}

/// Construct the rig-backed client from the loaded config. Returns
/// `None` (with a warning logged) when the API key is missing, so the
/// rest of the server boots cleanly without an agent. The HTTP routes
/// surface a clear error when the client is `None`.
pub fn build_client(config: &AgentConfig) -> Option<AgentClient> {
    if !config.is_configured() {
        warn!(
            "AGENT_API_KEY not set; agent endpoints will return 503. \
             Set AGENT_PROVIDER + AGENT_PRIMARY_MODEL + AGENT_API_KEY to enable."
        );
        return None;
    }
    match AgentClient::new(config) {
        Ok(c) => {
            info!(
                provider = c.provider_name(),
                model = c.primary_model(),
                "agent client constructed"
            );
            Some(c)
        }
        Err(e) => {
            warn!(error = %e, "failed to construct agent client; agent endpoints disabled");
            None
        }
    }
}
