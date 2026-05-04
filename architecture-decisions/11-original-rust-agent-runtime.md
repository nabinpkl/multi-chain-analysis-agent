# 11: Original Rust agent runtime (superseded)

This ADR records the original decision to write the agent in Rust as
a tokio task in the same process as the data plane. It was the
load-bearing decision for the entire chain-analysis-agent through
ships 1 through 5a. ADR 13 superseded it on 2026-05-03; the Rust
agent code was deleted in commit `04b7141`.

Recorded here so the iteration history is visible: we shipped Rust,
learned what didn't work, and migrated. The portfolio story is
"made a defensible call, then re-evaluated when the assumptions
changed", not "guessed Python from the start."

## Status

Superseded by ADR 13 (Python agent migration), 2026-05-03.
Implementation removed in Phase C of the migration (commit
`04b7141`).

## Problem

Building an LLM agent that:
- Reads from a live in-memory graph (`GraphState`,
  `Arc<RwLock<...>>` already in process)
- Composes typed primitives over that graph
- Streams claims to the frontend via SSE
- Runs on a single Oracle free-tier VM (24GB RAM, 4 Ampere cores)
- Has bounded cost (cost-as-rate-limit, not RPS)

Two coupled questions had to resolve before any code:
1. Where does the agent process run?
2. What LLM client library does it use?

## Decision

Originally locked in `docs/agent-design/01-agent-overview.md` as
**D-1** and **D-2**.

### D-1: same Rust process as ingest + analytics

Agent runs as a tokio task alongside the existing ingest and
analytics tasks. Shared `Arc<RwLock<GraphState>>`. SSE channel from
agent to frontend. Same ClickHouse instance, dedicated read-only
role.

### D-2: `rig` crate as LLM client; pinned model identifiers

LLM client is `rig` (provider-agnostic Rust crate with native
support for ~20 providers). Provider and model selected at deploy
time via configuration. Model identifiers pinned in Rust constants
per environment. No floating "latest" aliases. Two model slots:
primary reasoning model + cheap output-policy model.

## Rationale (at the time of the decision)

Three drivers:

1. **Live primitives need cheap GraphState access.** The graph is
   already an in-process `Arc<RwLock<...>>`. A separate agent
   service would add a localhost RPC hop and require exposing the
   live graph via an internal API, duplicating the existing
   analytics-task pattern. Same-process is the simplest shape.

2. **Single-binary deploy story on Oracle free tier.** One Rust
   binary, one `systemd` unit, one cloudflared sidecar. Adding a
   second service would mean two units, two health checks, an
   intra-machine network hop, two log streams. Marginal complexity
   that didn't earn its keep at the time.

3. **`rig` was the most credible Rust LLM client.** v0.36 had
   provider-agnostic abstractions for OpenAI, Anthropic, OpenRouter,
   etc. Active development. The alternative was rolling our own
   provider abstraction.

The cost-amplification risk that often motivates a separate process
(agent runaway pegging the host) was addressed by the per-principal
budget bucket framework (phase 05), not by a process boundary.

## Why this was later overridden (ADR 13)

Three things shifted between the original decision and 2026-05-03.

1. **`rig-core` stayed pre-1.0 with sparse activity.** No
   maintained Rust LLM client cleared `AGENTS.md`'s library bar
   (latest release within 1 month, real human triage, not bot-only
   churn). The Python ecosystem (Pydantic AI, instructor,
   LangGraph, OpenRouter clients) was the actual industry path.

2. **LiteLLM was excluded** after its March 2026 supply-chain
   attack and April 2026 SQLi (CVE-2026-42208), eliminating the
   natural cross-vendor proxy alternative.

3. **The single-process argument never applied to LLM-fronted
   code paths.** Turn latency is dominated 99.9% by the model
   call itself. A localhost JSON hop between a Python agent and
   the Rust primitive layer is invisible at that scale. D-1 was
   load-bearing for primitive *compute* (still in the Rust
   process today), not for the LLM loop. We had been treating
   one argument as if it applied to two different code paths.

The iteration speed cost compounded across ships 5b and 5c, which
were going to be mostly more prompts. Every prompt change was a
multi-minute `cargo rebuild`. In Python it's hot-reload via
uvicorn. The cost was paid forward.

## Consequences (the original ones, in hindsight)

### Accepted at the time, now resolved by ADR 13

- Bound to Rust LLM client ecosystem maturity. Resolved by moving
  to the Python ecosystem.
- Prompt iteration speed limited by `cargo rebuild`. Resolved by
  hot-reload via uvicorn.
- Agent test infrastructure in Rust. Replaced by pytest +
  pydantic-ai test models in ADR 13.

### What survived

- The data plane stayed in Rust (ingestion, graph window, primitive
  compute, snapshot lease, analytics). D-1's argument about
  GraphState access still holds for those.
- The protobuf wire format introduced for the migration is now the
  industry-standard shape regardless of agent language.
- The two-model-slot pattern (primary + policy) survived; only the
  client library changed.
- The pinned-model-identifier discipline survived; OpenRouter
  identifiers in Python constants now instead of Rust constants.

## What's left of D-1 and D-2

D-1 fragment that survived: "primitive compute lives in the same
process as `GraphState`." Still true.

D-2 wholly replaced: `rig` is gone, deleted with the rest of the
Rust agent module in commit `04b7141`. Pydantic AI is the agent
framework now (ADR 13).

## References

- `docs/agent-design/01-agent-overview.md`  original D-1 and D-2
  formulations live here, in the locked-in design context
- ADR 13 (`13-python-agent-migration.md`)  the override; spells
  out the migration rationale and cutover ritual in detail
- `AGENTS.md` "Library maintenance bar"  the discipline that
  eventually disqualified `rig-core` and ruled out LiteLLM
- Commit `04b7141`  Phase C deletion of the Rust agent module +
  drop of `rig-core` from `Cargo.toml`
