# docs

The repo's documentation lives here. Two top-level docs plus seven folders.

## Top-level files

- [design.md](design.md): source of truth for visual decisions on the frontend. Color tokens, type scale, component conventions. All tokens live in `frontend/src/app/globals.css`; this document explains the reasoning.
- [evals.md](evals.md): a frontier eval-review snapshot from May 2026. Aligned vs shallow vs missing, with gaps tagged by whether they are worth filing.

## Folders

### [agent-design/](agent-design/)

The agent's design across ships: build order, agent overview, the typed primitive layer, the agent loop, action ledger, rate-limiting and token cost, the eval suite, polish and analyst surfaces, and the forward-looking proactive Pulse work. Numbered 00 through 08 in build order.

### [securing-agents/](securing-agents/)

Transferable lessons on securing LLM agents, with our Solana analyst as the worked example. Five chapters plus an overview: the `<external_data>` envelope and its escape, the user-input topical rail, the output verification pipeline, domain and identity discipline, and per-defense ablation plus runtime parity. Each chapter cites the unit tests and eval cases that pin the defense in our codebase.

### [architecture/](architecture/)

Architecture notes that are too specific for the agent-design ships and too general for an inline code comment. Today: the token-metadata ingestion path.

### [research/](research/)

Background research that informed the build. Today: the multi-hop injection study that shaped the prompt-injection defense surface.

### [learnings/](learnings/)

Pattern notes I collected while building. Mostly Rust performance and data-pipeline things: Vec vs HashMap, the generation-tagging pattern, interning, JIT warmup, agentic database access, change-data-capture, debouncing, read-vs-write locks, raw-graph findings.

### [EngineeringPosts/](EngineeringPosts/)

LinkedIn-style engineering posts written from the build. Why Rust, why ClickHouse, how I use the Governor pattern in Rust, designing poll endpoints that do not collapse under load, Kappa-architecture and CQRS, and the concepts that show up across the posts. Style rules for this folder live in [../AGENTS.md](../AGENTS.md).

## Where to start

If you are reading the codebase for the first time, the order that makes sense to me:

1. [../AGENTS.md](../AGENTS.md) for the repo-wide rules and the project intent.
2. [agent-design/00-build-order.md](agent-design/00-build-order.md) for the ship-by-ship narrative.
3. [agent-design/01-agent-overview.md](agent-design/01-agent-overview.md) for the agent's place in the system.
4. Whichever folder matches what you are touching: securing-agents for prompt-injection work, architecture or research for the data plane, learnings for individual performance patterns.

If you are reading to understand a specific security concern, jump straight to [securing-agents/00-overview.md](securing-agents/00-overview.md). The threat-model-to-defense table there points at the chapter that covers each attack surface.
