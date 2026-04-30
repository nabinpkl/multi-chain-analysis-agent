//! Smoke test for the rig + OpenRouter wiring. Constructs a rig
//! OpenRouter client directly (bypassing our `AgentClient` wrapper so
//! this binary stays free of any internal lib dependency). Sends a
//! minimal completion against the configured model, prints the
//! response. Confirms the API key works before booting the server.
//!
//! Usage from `backend/`:
//!   cargo run --bin agent_smoke
//!
//! Requires the same env vars the server reads:
//!   AGENT_API_KEY        provider API key (required)
//!   AGENT_PRIMARY_MODEL  model id (defaults to the Nemotron free model)
//!
//! The full agent wrapper (`AgentClient` in `agent/client.rs`) takes
//! the same path; if this smoke runs, the server's path runs.

use anyhow::{Context, Result, anyhow};
use rig::client::CompletionClient;
use rig::completion::Prompt;
use rig::providers::openrouter;

const PREAMBLE: &str = "\
You are a read-only analyst agent for a Solana transaction graph. \
Reply briefly so we can confirm the LLM client is online.";

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    let api_key = std::env::var("AGENT_API_KEY")
        .map_err(|_| anyhow!("AGENT_API_KEY required (set in .env or environment)"))?;
    let model = std::env::var("AGENT_PRIMARY_MODEL")
        .unwrap_or_else(|_| "nvidia/nemotron-3-super-120b-a12b:free".into());

    println!("smoke test: provider=openrouter model={model}");

    let client = openrouter::Client::new(&api_key)
        .context("constructing rig openrouter client")?;
    let agent = client.agent(&model).preamble(PREAMBLE).build();
    let response = agent
        .prompt("Say hello in one short sentence so we know you're online.")
        .await
        .context("rig agent prompt failed")?;

    println!("\n=== response ===\n{response}\n=== end ===");
    Ok(())
}
