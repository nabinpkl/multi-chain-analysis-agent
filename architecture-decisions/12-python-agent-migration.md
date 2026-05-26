# 13: Migrating the agent plane to Python (Pydantic AI)

This document records the decision to split the agent runtime out of
the Rust process into a separate Python service, why the split is
worth its cost, and what it overrides from
`docs/agent-design/01-agent-overview.md`.

## Status

Accepted, 2026-05-03. Shipped across commits `502c442` (Phase I lock
invariants), `82b2c11` (Phase II Python agent loop end-to-end),
`04b7141` (Phase C delete Rust agent module + drop rig-core).

## Problem

The agent shipped in 01 ran entirely in Rust against `rig-core` v0.36
(pinned, pre-1.0). Two compounding pains surfaced once the design
moved past the walking-skeleton phase:

1. **Ecosystem.** The serious agent tooling is Python: Pydantic AI,
   instructor, LangGraph, the OpenRouter client, the cross-vendor
   eval harnesses. Maintained Rust LLM clients effectively do not
   exist at the bar in `AGENTS.md` (1-month release cadence, real
   human triage, no bot-only churn). `rig-core` is the only credible
   option and it is pre-1.0 with sparse activity. LiteLLM was
   considered as the cross-vendor proxy and excluded after its
   March 2026 supply-chain attack and April 2026 SQLi
   (CVE-2026-42208).
2. **Iteration speed.** Every prompt change was a `cargo rebuild`
   cycle. Ship 5b (warehouse primitives + ground-truth re-query) and
   ship 5c (orchestrator + self-review) are mostly more prompts.
   Building them in Rust compounded the cost.

The original "single Rust binary" decision (D-1 in `01-*`) was
load-bearing for graph reads. It was never load-bearing for
LLM-fronted code paths because turn latency is dominated 99.9% by
the LLM call itself; a localhost JSON hop between a Python
orchestrator and the Rust primitive layer is invisible.

## Decision

Two services, no proxy, no compat layer. Each owns one concern
end-to-end.

```
                +-------------------------+
                |  Frontend (Next.js)     |
                +------+----------+-------+
                       |          |
        graph + health |          | /agent/* (SSE)
                       v          v
   +-------------------+--+   +---+----------------------+
   |  Rust :8002          |   |  Python :8003            |
   |  (data plane)        |   |  (agent plane)           |
   |                      |   |                          |
   |  - ingestion         |   |  - Pydantic AI loop      |
   |  - graph window      |   |  - structural gate       |
   |  - /turn/{begin,end} |<--+  - constitution gate     |
   |  - /primitive/*      |   |  - SSE emission          |
   |  - snapshot lease    |   |  - thread state (mem)    |
   +----------------------+   |  - prompts (.txt)        |
              |               |  - ledger writer         |
              | graph data    +-------------+------------+
              v                             |
   +----------------------+                 | ledger events
   |  ClickHouse          |<----------------+
   |  - graph tables      |
   |    (Rust writes)     |
   |  - agent_ledger      |
   |    (Python writes)   |
   +----------------------+
```

### Wire types

Single source of truth in protobuf (`proto/multichain/wire/`),
codegen to Rust (`buffa`), Python (`google.protobuf`), TypeScript
(`@bufbuild/protoc-gen-es`). Hand-written wire types are a bug.
Wire format per hop:

| Hop | Format | Why |
|---|---|---|
| Browser to Python `/agent/*` | proto canonical JSON | EventSource is text-only |
| Browser to Rust `/health`, `/graph/*` | proto canonical JSON | curl-debuggable |
| Python to Rust `/primitive/*`, `/turn/*` | binary protobuf | both speak proto natively |

Locked in `AGENTS.md`.

### Ledger ownership

Python owns `multichain.agent_ledger` end-to-end via
`clickhouse-connect` (official ClickHouse Inc client, passes the
maintenance bar). Rust has zero agent-ledger code. ClickHouse has
two writers writing to disjoint tables; no contention.

### Cutover ritual

One commit, atomic. No proxy, no env-var switch between old and new
backend, no "support both for a few weeks." When Phase C landed,
Rust agent code deleted in the same commit, frontend pointed at
Python, done. Per `AGENTS.md` "no backward compat layers."

## What this overrides

From `docs/agent-design/01-agent-overview.md`:

| Original | Now |
|---|---|
| **D-1** Same Rust process, separate tokio task | Separate Python service on `:8003`. Rust on `:8002` keeps ingestion + graph + primitive compute. |
| **D-2** `rig` crate as LLM client | Pydantic AI agent definitions; `openai` SDK pointed at OpenRouter via Pydantic AI's `OpenAIProvider`. Same two model slots (primary + policy), same pinning rule. |
| **D-4** `Claim` typed via `ts-rs` from Rust struct | `Claim` defined in `proto/multichain/wire/agent/v1/*.proto`, generated to Rust + Python + TS. The "tagged provenance enum" semantics are unchanged; the source moved from Rust to proto. |

D-3, D-5, D-6, D-7 unchanged. The conversation surface, the
three-source split, the disambiguation principle, and the reactive
+ proactive mode split are all agent-plane concerns that survived
the language move.

The six locked invariants in 01 also survive unchanged. Read-only
typed primitives, three-layer untrusted text defense,
provenance-attached claims, anonymous principal, cost-as-rate-limit,
action ledger + eval. Nothing here weakens any of them; the only
shift is which process enforces which.

## Rationale

Five drivers, in decreasing weight.

### 1. The agent ecosystem is in Python

Pydantic AI gives us typed agent definitions, structured output via
Pydantic models, built-in `UsageLimits` for free-tier OpenRouter,
and a maintainer team with a public security posture. The Python
side also unlocks the eval harness ecosystem (out of scope for this
plan, follow-up). None of this exists in Rust at the maintenance
bar in `AGENTS.md`.

### 2. LiteLLM is excluded

The natural alternative to rolling our own provider abstraction is
LiteLLM. After the March 2026 supply-chain attack and April 2026
SQLi (CVE-2026-42208), it does not pass the maintenance bar.
Recorded in `AGENTS.md` as a concrete anti-pattern. Pydantic AI
plus the `openai` SDK pointed at OpenRouter is the smaller,
auditable alternative.

### 3. Iteration speed dominates LLM-fronted code

Every turn-shape change in the agent (prompt rev, structural gate
tweak, repeat-detector tuning) is a multi-minute `cargo rebuild` in
Rust and a process restart. In Python it is hot-reload via uvicorn.
The cost compounds through ship 5b and 5c, which are mostly more
prompts and more gate logic.

### 4. The single-binary argument does not apply

D-1's rationale was that primitive compute needs cheap access to
`GraphState` (an `Arc<RwLock<...>>` already in process). That is
still true and that code did not move. Only the agent loop moved.
Turn latency is dominated 99.9% by the LLM call; a localhost JSON
hop to the primitive endpoints is invisible. The cost-amplification
risk that D-1 also cited (agent runaway pegging the host) is
addressed by the budget framework in phase 05, which is unchanged.

### 5. Snapshot lease cleanly resolves cross-primitive read consistency

The original in-process design relied on the lock being held for the
duration of a turn; with the agent out of process, that breaks. The
snapshot lease (`POST /turn/begin` returns a `snapshot_id` referring
to a materialized 60s `WindowSnapshot`; primitives in the turn pass
the id; `POST /turn/end` releases) gives us byte-identical primitive
output across calls in one turn even if a new block ingests
mid-turn. The same call will return the same bytes; ledger replay
and diff walking become deterministic. Strict improvement over the
implicit consistency of the in-process design.

## Consequences

### Accepted

- Two services to deploy. Both run on the same VM (Oracle free tier,
  unchanged). Cost is one extra `docker compose` block and one extra
  health check.
- Two `NEXT_PUBLIC_*` env vars on the frontend
  (`NEXT_PUBLIC_API_URL` and `NEXT_PUBLIC_AGENT_URL`).
- ClickHouse `agent_ledger` schema is owned by Python now. The
  cutover dropped the old Rust-shaped table; Python recreates on
  boot per "all data refreshes" in `AGENTS.md`. No migration
  script.
- Snapshot leases are process-local in Rust; on Rust restart all
  leases vanish. Python sees 410-equivalent on the next primitive
  call, retries `/turn/begin`. Acceptable for v0.
- Thread state is in-memory in the Python service; on Python
  restart, in-flight threads vanish. Same posture as the existing
  `thread.in_memory_only` stub. Persistent thread state is a
  separate ship.
- `rig-core` is gone from `Cargo.toml`. Re-adopting any Rust LLM
  client would re-open this ADR.

### Rejected

- **Keep Rust agent, port to a maintained Rust LLM client.** None
  exists at the bar.
- **LiteLLM proxy in front of either backend.** Security exclusion.
- **LangGraph instead of Pydantic AI.** Per separate analysis,
  Pydantic AI handles ship 5b and 5c shapes more cleanly. Revisit
  if the orchestration shape outgrows simple agent + tools + typed
  output.
- **Backward-compat env-var switch between old Rust agent and new
  Python agent.** AGENTS.md "no backward compat" rule. The cutover
  was atomic in `04b7141`.
- **Hand-typed wire shapes per language.** `AGENTS.md` rule. Three
  codegen flows from one proto source.

## Implementation surface

### Rust (post-Phase C)

- `backend/src/primitives/{wallet_profile,community_summary,types,mod}.rs`
  (renamed from `backend/src/agent/primitives/`). Pure compute
  functions; the `Primitive` trait, registry, and `agent_stubs.hit`
  are gone.
- `backend/src/snapshot.rs` (renamed from `backend/src/agent/snapshot.rs`).
  Snapshot lease + GC sweep.
- `backend/src/wire/proto_bridge.rs`. Proto types to internal Rust
  shapes. The "TRANSITIONAL" framing in the original commit message
  was dropped; this is the steady-state shape.
- `backend/src/api/`. Routes: `/health`, `/ready`, `/graph/*`,
  `/turn/{begin,end}`, `/primitive/*`. No `/agent/*`.
- Deleted: `backend/src/agent/` (entire directory),
  `backend/src/api/agent.rs`, `backend/src/api/diagnostics.rs`,
  `backend/src/bin/agent_smoke.rs`. `rig-core`, `async-stream`,
  `fastrand`, `schemars`, `sha2`, `regex` dropped from
  `Cargo.toml`.

### Python (`agent-service/src/agent_service/`)

- `agent.py`. Primary Pydantic AI `Agent`, three tools
  (`wallet_profile`, `community_summary`, `emit_claim`), system
  prompt loaded from `prompts/system_v4.txt`,
  `output_type=str` for the narrative channel.
- `loop_driver.py`. Turn orchestration: thread lock, repeat
  detection, snapshot lease, agent run, per-claim structural gate,
  per-claim constitution gate, narrative gate, thread state update,
  ledger writes, terminal frame.
- `policy/{placeholder,structural,crosscheck,binding_store,constitution}.py`.
  Direct ports of the Rust gate stack; same thresholds, same
  resolution order, same first-error semantics.
- `repeat_detector.py`, `diff.py`. Ship 4 don't-repeat-yourself
  paths.
- `thread_state.py`. `ThreadRegistry` with outer asyncio lock for
  registry mutation, per-thread inner asyncio lock for turn
  execution. `MAX_THREAD_CLAIMS=20`,
  `MAX_THREAD_TOOL_CALL_TURNS=20` (same constants as Rust).
- `ledger/writer.py`. `clickhouse-connect` async client, idempotent
  `CREATE TABLE`, per-session sequence counter under asyncio lock,
  sha256 of canonical JSON. Errors logged via structlog and
  swallowed (matches existing semantics that "flaky ClickHouse
  cannot kill an in-flight session").
- `boundary.py`. Proto canonical JSON to pydantic at the HTTP edge.
- `main.py`. FastAPI app, lifespan builds three agents +
  `PrimitiveClient` + `ThreadRegistry` + `Ledger`, exposes
  `/health`, `/agent/ask`, `/agent/stream/{session_id}`.

### Frontend

- `frontend/src/hooks/use-agent-stream.ts` reads
  `NEXT_PUBLIC_AGENT_URL` for `/agent/*` calls.
- All wire types from generated proto bindings under
  `frontend/src/lib/wire/`.

## Verification

Per-phase verification ran during the migration:
- Phase I: SSE byte-golden diff zero across all 9 frame variants.
- Phase B.3: Python structural gate produces same approve/retract
  on a fixture set the Rust gate also runs.
- Phase B.4: constitution gate verdict shape matches Rust on
  representative claims.
- Phase B.7: ledger rows appear in ClickHouse from Python writes;
  100-row burst smoke passed.
- Phase C: `grep -r "use rig" backend/` returns zero.
  `cargo build --bin server` green. `docker compose up -d --build`
  green. Manual probe through frontend confirms wire shape
  unchanged.

## References

- `docs/agent-design/01-agent-overview.md` (the document this ADR overrides D-1, D-2,
  partially D-4 of).
- `AGENTS.md`, sections "Wire type ownership" (proto single source
  of truth), "Library maintenance bar" (LiteLLM exclusion), "Wire
  format per hop" (binary vs canonical JSON matrix).
- Pydantic AI documentation
  (https://ai.pydantic.dev), `UsageLimits`, structured-output via
  pydantic models, `RunContext.deps`.
- `clickhouse-connect` (official ClickHouse Inc Python client,
  MIT, async support).
- LiteLLM CVE-2026-42208 (April 2026), March 2026 supply-chain
  attack. Both linked from `AGENTS.md`.
- OpenRouter API docs (https://openrouter.ai/docs), used via
  Pydantic AI's `OpenAIProvider` with `base_url` override.
- Buf CLI, `buffa`, `@bufbuild/protoc-gen-es`. The three codegen
  toolchains pinned in `AGENTS.md`.
- Ship commits: `502c442`, `82b2c11`, `04b7141`. Closed migration
  issues: #1, #2, #3, #4, #5, #6, #7, #8, #9, #10, #11, #12, #13,
  #14.
