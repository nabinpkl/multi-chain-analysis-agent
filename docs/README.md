# docs

Long-form documentation. The root-level docs are the higher-priority entry points; this folder is the per-subsystem deep dives behind them.

Before this folder, read at the repo root:

- [../PRD.md](../PRD.md). What this project is, what it is not, and what is in scope right now.
- [../SPEC.md](../SPEC.md). How it is built: contracts, invariants, source-of-truth pointers.
- [../AGENTS.md](../AGENTS.md). Repo rules; read before opening a PR.

This folder grew by accretion. The boundaries between learnings, research, and engineering posts were blurry, and the "Why X" decision docs ended up sitting with the engineering posts instead of with the architecture they were actually deciding. The current layout is the cleanup pass: pattern notes and external reading merged into one `references/` folder, blog-style posts in `engineering-blogs/`, and the "Why X" decision docs moved to `architecture/` where they belong.

## Top-level files

- [design.md](design.md): source of truth for visual decisions on the frontend. Color tokens, type scale, component conventions. All tokens live in `frontend/src/app/globals.css`; this document explains the reasoning.
- [evals.md](evals.md): a frontier eval-review snapshot from May 2026. Aligned vs shallow vs missing, with gaps tagged by whether they are worth filing.

## Folders

### [agent-design/](agent-design/)

The agent's design across ships, numbered 00 through 08 in build order. Start with `00-build-order.md` for the ship narrative; the rest are per-ship deep dives (agent overview, typed primitive layer, agent loop and injection defense, action ledger, rate-limiting and token cost, the eval suite, polish, and forward-looking proactive Pulse work).

### [securing-agents/](securing-agents/)

Transferable lessons on securing LLM agents, with our Solana analyst as the worked example. Five chapters plus an overview. Each chapter cites the unit tests and eval cases that pin the defense in our codebase. Start at `00-overview.md`; the threat-model-to-defense table there points at the chapter for each attack surface.

### [architecture/](architecture/)

Architecture and architectural-decision docs. The "why X" documents (`WhyRust.md`, `WhyClickHouseDB.md`) capture the stack decisions; the rest cover specific subsystems (today: token-metadata ingestion).

### [references/](references/)

Pattern notes, research summaries, and anything I learned while building that didn't have an obvious home. Mostly Rust performance and data-pipeline patterns: Vec vs HashMap, generation-tagging, interning, JIT warmup, agentic DB access, CDC, debouncing, read-vs-write locks. Also an external-literature summary on multi-hop prompt injection.

### [engineering-blogs/](engineering-blogs/)

LinkedIn-style engineering posts written from the build. How I use the Governor pattern in Rust, designing poll endpoints that do not collapse under load, Kappa-architecture and CQRS, and the concepts that show up across the posts. Style rules for this folder live in [../AGENTS.md](../AGENTS.md).

## Where to start

If you are reading the codebase for the first time, the order that makes sense:

1. [../PRD.md](../PRD.md) for what this project is and is not.
2. [../SPEC.md](../SPEC.md) for the technical contracts and invariants.
3. [../AGENTS.md](../AGENTS.md) for repo-wide rules.
4. [agent-design/00-build-order.md](agent-design/00-build-order.md) for the ship-by-ship narrative with retros.
5. [agent-design/01-agent-overview.md](agent-design/01-agent-overview.md) for the agent's place in the system.
6. Whichever folder matches what you are touching.

If you are reading to understand a specific security concern, jump straight to [securing-agents/00-overview.md](securing-agents/00-overview.md).
