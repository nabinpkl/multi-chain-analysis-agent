//! Thin wrapper around rig's provider client. v0 supports OpenRouter
//! only; adding providers is a match arm. Per the prior architecture
//! decision: do not introduce our own `LlmClient` trait on top of rig.
//! Use rig's `Agent` builder directly; if we ever need a second
//! abstraction, add it then.

use anyhow::{Context, Result};
use rig::client::{CompletionClient, ProviderClient};
use rig::completion::Prompt;
use rig::providers::openrouter;

use super::config::AgentConfig;

#[derive(Clone)]
pub enum AgentClient {
    OpenRouter {
        client: openrouter::Client,
        primary_model: String,
        /// Cheap policy model used by the output-policy gate (phase 03
        /// layer 3, ship 2). Pinned per-env; swapped via
        /// `AGENT_POLICY_MODEL`. Distinct from `primary_model` so
        /// failures decorrelate.
        policy_model: String,
    },
}

impl AgentClient {
    pub fn new(config: &AgentConfig) -> Result<Self> {
        match config.provider.as_str() {
            "openrouter" => {
                let client = openrouter::Client::new(&config.api_key)
                    .context("rig openrouter client construction")?;
                Ok(Self::OpenRouter {
                    client,
                    primary_model: config.primary_model.clone(),
                    policy_model: config.policy_model.clone(),
                })
            }
            other => anyhow::bail!(
                "unsupported AGENT_PROVIDER {other:?}; v0 supports: openrouter"
            ),
        }
    }

    /// Single-shot completion against the primary reasoning model. v0
    /// round-trip used by the smoke binary. The agent loop uses rig's
    /// `Agent::prompt(...).with_history(...).max_turns(...)` directly
    /// rather than this helper.
    pub async fn complete(&self, system: &str, user: &str) -> Result<String> {
        match self {
            Self::OpenRouter {
                client,
                primary_model,
                ..
            } => {
                let agent = client
                    .agent(primary_model.as_str())
                    .preamble(system)
                    .build();
                let response = agent
                    .prompt(user)
                    .await
                    .context("rig agent prompt failed")?;
                Ok(response)
            }
        }
    }

    /// Single-shot completion against the cheap **policy** model with
    /// the constitution as preamble. Ship 2's `OutputPolicy` calls
    /// this for every Claim and every Narrative emission. Keeps the
    /// rig invocation in one place so adding providers (or a different
    /// abstraction over rig) doesn't fan out.
    pub async fn complete_policy(&self, system: &str, user: &str) -> Result<String> {
        match self {
            Self::OpenRouter {
                client,
                policy_model,
                ..
            } => {
                let agent = client
                    .agent(policy_model.as_str())
                    .preamble(system)
                    .build();
                let response = agent
                    .prompt(user)
                    .await
                    .context("rig policy prompt failed")?;
                Ok(response)
            }
        }
    }

    /// The primary model id currently in use. Surfaced on diagnostics
    /// + the LlmCallLogger hook log.
    pub fn primary_model(&self) -> &str {
        match self {
            Self::OpenRouter { primary_model, .. } => primary_model,
        }
    }

    /// The policy model id currently in use. Surfaced on diagnostics
    /// alongside `primary_model` so the operator can see which pair is
    /// active without reading env.
    pub fn policy_model(&self) -> &str {
        match self {
            Self::OpenRouter { policy_model, .. } => policy_model,
        }
    }

    pub fn provider_name(&self) -> &'static str {
        match self {
            Self::OpenRouter { .. } => "openrouter",
        }
    }
}
