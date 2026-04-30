//! Agent-specific configuration loaded from environment variables. Per
//! D-2 (overview): provider-agnostic via rig, model identifiers pinned
//! per environment. v0 supports openrouter only; adding a provider is
//! adding a match arm in `client.rs` plus a string here.

use std::env;

#[derive(Clone, Debug)]
pub struct AgentConfig {
    /// Provider name. v0 only `"openrouter"` is wired; others are
    /// future arms in `AgentClient::new`.
    pub provider: String,
    /// Pinned model identifier (provider-specific format).
    pub primary_model: String,
    /// Cheap model identifier for the output policy pass (phase 03
    /// layer 3). Unused in ship-0; the field carries forward.
    pub policy_model: String,
    /// Provider API key. Read from a single env var so swapping
    /// providers does not require renaming.
    pub api_key: String,
}

impl AgentConfig {
    pub fn from_env() -> Self {
        Self {
            provider: env::var("AGENT_PROVIDER").unwrap_or_else(|_| "openrouter".into()),
            primary_model: env::var("AGENT_PRIMARY_MODEL")
                .unwrap_or_else(|_| "nvidia/nemotron-3-super-120b-a12b:free".into()),
            policy_model: env::var("AGENT_POLICY_MODEL")
                .unwrap_or_else(|_| "nvidia/nemotron-3-super-120b-a12b:free".into()),
            api_key: env::var("AGENT_API_KEY").unwrap_or_default(),
        }
    }

    /// True when an API key is present. The agent runtime degrades
    /// gracefully when missing: it boots and logs a warning instead
    /// of failing the whole server start.
    pub fn is_configured(&self) -> bool {
        !self.api_key.is_empty()
    }
}
