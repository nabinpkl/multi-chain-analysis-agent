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
                })
            }
            other => anyhow::bail!(
                "unsupported AGENT_PROVIDER {other:?}; v0 supports: openrouter"
            ),
        }
    }

    /// Single-shot completion. v0 round-trip used by both the smoke
    /// binary and the SSE stub. Future ships replace this with a tool-
    /// using loop and a streaming variant.
    pub async fn complete(&self, system: &str, user: &str) -> Result<String> {
        match self {
            Self::OpenRouter {
                client,
                primary_model,
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

    /// The model id currently in use. Surfaced on stub claims so the
    /// frontend can show "answered by <model>" during ship-0.
    pub fn primary_model(&self) -> &str {
        match self {
            Self::OpenRouter { primary_model, .. } => primary_model,
        }
    }

    pub fn provider_name(&self) -> &'static str {
        match self {
            Self::OpenRouter { .. } => "openrouter",
        }
    }
}
