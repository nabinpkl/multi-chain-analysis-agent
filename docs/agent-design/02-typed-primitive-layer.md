# 02: Typed primitive layer

The set of read-only operations the agent is allowed to perform.
Every analysis the agent can produce is a composition of these
primitives. The agent never authors SQL, never holds a database
connection, never executes arbitrary code.

## Problem

A free-form text-to-SQL agent fails on three independent axes:
correctness (hallucinated schema, joins on the wrong key), cost
(unbounded query plans), and security (SQL injection from
attacker-controlled input). Each is a known failure mode with a long
incident history.

The alternative is a typed action layer: a small, declarative kit of
operations with statically-known input schemas, output schemas,
data-source bindings, and cost classes. The agent picks operations
and supplies arguments. The runtime validates, executes, and returns
typed results.

This phase defines that kit, the shapes that flow through it, and
the executor that runs each primitive against the right backend.

## Industry standards

The typed primitive layer is the dominant pattern for production LLM
agents that touch real data:

- **Vendor function-calling APIs.** OpenAI function calling,
  Anthropic tool use, Google Gemini function calling, and others.
  The shape has converged across providers: tool definitions with
  JSON-schema-validated arguments; the model emits a validated
  call. Our LLM client (`rig`) abstracts over them; the typed
  primitive layer is the same shape regardless of which provider
  serves.
- **Model Context Protocol (MCP).** Open protocol for exposing
  tools to a model from an external server. Originally from
  Anthropic and now supported by multiple clients. Same shape at
  the wire level. Useful as a future extension if we want to
  expose primitives to other model clients (e.g. a Python research
  client).
- **Capability-based security.** Each tool is a capability; the
  agent has only the capabilities the runtime hands it. No tool means
  no access. Original literature: Dennis & Van Horn, 1966; modern
  surveys in Miller, "Robust Composition" (2006).
- **Read replicas for analytics.** ClickHouse, Postgres, and most
  warehouses support read-only roles at the connection level. The
  agent's connection uses a role that can `SELECT` and nothing else.
  Reference: ClickHouse `CREATE USER ... DEFAULT ROLE = readonly`,
  `READONLY = 1` profile.

The OWASP LLM07 (Insecure Plugin Design) and LLM08 (Excessive Agency)
guidance is the most direct framing: each tool's surface area is a
plugin; minimizing the surface is the entire defense.

## Open questions

1. **Final primitive set.** The candidate set below is the working
   draft. Before code lands, we commit to a numbered list. Adding a
   primitive later is a design change, not a routine extension.

2. **`time_window_diff` shape.** The most useful diff is "what
   changed between window A and window B" but the output schema is
   non-obvious: per-wallet deltas, per-community deltas, or a
   structured diff object? Resolve before implementation.

3. **`tag_lookup` data source.** v0 returns the role/community labels
   we already produce internally. External tag sources (Helius,
   Solscan, hand-curated lists) are explicit future work, deferred
   to phase 07. The primitive's signature is locked now so future
   sources slot in without an agent-prompt change.

4. **Pagination convention.** Long results (e.g.
   `community_members` of a 5000-member component) need a stable
   pagination shape. Cursor-based with an opaque token? Offset +
   limit? Pick one and apply uniformly.

5. **Error taxonomy.** Some errors are recoverable by the agent
   (budget exceeded, retry with smaller window) and some are not
   (input validation failure, internal error). The error type
   surfaced to the agent should distinguish these so the agent's
   recovery logic doesn't burn tokens on unrecoverable errors.

## Approach

Each primitive is a Rust function with a typed `Input` and `Output`
struct, both `serde::Serialize + Deserialize` and exported to
TypeScript via `ts-rs`. The executor:

1. Receives an `(operation_name, arguments_json)` from the agent.
2. Looks up the operation in a registry; rejects unknown names.
3. Deserializes arguments against the `Input` schema; rejects
   schema-invalid input.
4. Checks the budget (phase 05); rejects if pre-flight cost exceeds
   remaining budget.
5. Runs the primitive against the right backend.
6. Serializes the `Output`; returns to the agent as a tool result.
7. Logs the call to the action ledger (phase 04).

Primitives split into three families by data source (per D-5 in
the overview):

- **Live primitives** read `GraphState` under a brief read lock.
  Cheap (sub-millisecond to low milliseconds). Pattern matches the
  existing analytics task's snapshot helper
  (`backend/src/analytics/snapshot.rs`).
- **Warehouse primitives** issue parameterized SQL against ClickHouse.
  Moderate to expensive. Use `EXPLAIN ESTIMATE` for pre-flight cost
  estimation (phase 05). Connection uses the read-only role with
  `max_execution_time` and `max_rows_to_read` set per query.
- **External primitives** call third-party tag sources (Helius,
  Solscan, etc.). Network latency, third-party trust boundary,
  per-source rate limits. Deferred to phase 07; the `tag_lookup`
  signature is locked now so external sources slot in without an
  agent-prompt change.

### Temporal axis: `TimeScope`

Primitives with temporal semantics take a required `TimeScope`
argument:

```rust
pub enum TimeScope {
    Live,                              // current rolling window
    Range { from_ms: u64, to_ms: u64 },// absolute block_time range
}
```

`Live` routes to `GraphState`; `Range` routes to ClickHouse via the
warehouse path. The agent cannot call a temporal primitive without
committing to a frame. This is the type-level expression of D-6's
disambiguation principle: the temporal decision is auditable in the
action ledger (phase 04) instead of hiding inside the model's
reasoning.

Per-primitive temporal support:

| Primitive | `Live` | `Range` |
|-----------|:------:|:-------:|
| `wallet_profile` | yes | yes |
| `neighborhood` | yes | yes |
| `community_members` | yes | no (communities are derived from the live window) |
| `path_between` | yes | yes |
| `top_by_metric` | yes | yes (warehouse-backed) |
| `time_window_diff` | no | required (two `Range`s) |
| `tag_lookup` | n/a (atemporal) | n/a |

The few `Range`-bearing primitives are the ones that pay close
attention to cost: `EXPLAIN ESTIMATE` runs first (phase 05), the
result gates the call. Live calls are cheap enough to skip the
pre-flight.

### Candidate primitives

The set below is the working draft. v0 commits exactly to this list;
extension is a phase-09 conversation, not an inline change.

#### `wallet_profile`
- **Input:** `{ addr: String, time_scope: TimeScope }`
- **Output:** `{ role: NodeRole, community_id: Option<u32>,
  stats: NodeStats, top_counterparties: Vec<(addr, edge_count,
  volume)>, age_in_window_secs: u64 }`
- **Source:** live (on `Live`) or warehouse (on `Range`)
- **Cost class:** cheap (`Live`) / moderate (`Range`)
- **Provenance:** wallet idx, top-counterparty edge ids

#### `neighborhood`
- **Input:** `{ addr: String, depth: u8 (1..=2), time_scope: TimeScope,
  max_nodes: u32 (capped at 500) }`
- **Output:** typed subgraph with nodes, edges, per-node stats
- **Source:** live (`Live`) or warehouse (`Range`)
- **Cost class:** moderate (depth=1) / expensive (depth=2)
- **Provenance:** every node and edge in the returned subgraph

#### `community_members`
- **Input:** `{ community_id: u32, page: PageCursor }` (live-only;
  communities are computed from the current live window)
- **Output:** `{ members: Vec<MemberSummary>, next_cursor:
  Option<PageCursor>, community_stats: CommunityStats }`
- **Source:** live
- **Cost class:** moderate
- **Provenance:** community id, member ids, edge ids backing the
  stats

#### `path_between`
- **Input:** `{ src: String, dst: String, max_hops: u8 (1..=4),
  time_scope: TimeScope }`
- **Output:** `Option<{ path: Vec<addr>, edges: Vec<EdgeRef>,
  total_volume: f64 }>`
- **Source:** live BFS over snapshot (`Live`) or warehouse path
  reconstruction (`Range`)
- **Cost class:** expensive
- **Provenance:** every node and edge on the path

#### `top_by_metric`
- **Input:** `{ metric: Metric, role_filter: Option<NodeRole>,
  time_scope: TimeScope, n: u32 (capped at 100) }`
- **Output:** `Vec<{ addr, value: f64, supporting_stats }>`
- **Source:** live (`Live`) or warehouse (`Range`)
- **Cost class:** moderate (`Live`) / expensive (`Range`,
  EXPLAIN-gated)
- **Provenance:** ranked addrs and the metric's supporting numbers

#### `time_window_diff`
- **Input:** `{ from: TimeScope::Range, to: TimeScope::Range,
  metric: DiffMetric }`
- **Output:** structured diff (TBD per open question 2)
- **Source:** warehouse only (no `Live` form; live diffs are a
  follow-up question, not a primitive)
- **Cost class:** expensive (EXPLAIN-gated)
- **Provenance:** time ranges, aggregated row counts

#### `tag_lookup`
- **Input:** `{ addrs: Vec<String> (capped at 100) }`
- **Output:** `Vec<{ addr, tags: Vec<TagEntry> }>`
- **Source:** internal labels (v0); external sources deferred
- **Cost class:** cheap
- **Provenance:** tag id and source per entry

### Cost-class taxonomy

A tag on each primitive that the budget layer (phase 05) consumes:

- **cheap:** sub-millisecond live read, fixed cost. Examples:
  `wallet_profile`, `tag_lookup`.
- **moderate:** scales with neighborhood / community size, bounded by
  the input cap. Examples: `community_members`, `top_by_metric`,
  `neighborhood(depth=1)`.
- **expensive:** scales with the entire window or hits the warehouse.
  Pre-flight EXPLAIN required for warehouse primitives. Examples:
  `neighborhood(depth=2)`, `path_between`, `time_window_diff`.

The cost class is declared in primitive metadata, not measured at
runtime. Phase 05 reads this tag for budget pre-flight.

### Provenance shape

Every primitive output carries enough identifiers for the agent's
output to be checkable:

```rust
pub enum ProvenanceRef {
    Wallet { addr: String, idx: NodeIdx },
    Edge   { id: EdgeId, src: NodeIdx, dst: NodeIdx },
    Community { id: u32, window: WindowSeconds },
    TimeRange { from: u64, to: u64 },
    Number { metric: String, value: f64, support: Vec<EdgeId> },
}
```

The agent attaches `Vec<ProvenanceRef>` to each `Claim` it emits
(phase 03). The UI renders refs as interactive chips that highlight
or focus the corresponding entity on the live graph.

### Tool description for the LLM

Each primitive has a hand-written description shipped to the model
in the `tools` array. The description includes:
- One-paragraph plain-English summary of what it does.
- One sentence on when to use it vs the alternatives.
- Argument shapes (auto-derived from the JSON schema).
- Cost class hint ("cheap, free to call freely", "expensive, prefer
  to use only when narrower options would not work").
- A line on the budget impact ("consumes N budget units").
- For temporal primitives, **routing examples** that map question
  patterns to `TimeScope`:

  > Use `Live` when the user asks about "now", "current",
  > "right now", or "in the last minute", or refers to entities
  > visible in their current view. Use `Range { from, to }` when
  > they specify an absolute time, "yesterday", "last hour", or
  > compare against a different time. If the user is comparing two
  > periods, call this primitive twice with different scopes.

The descriptions live in code next to the primitive impl so they
travel together. A primitive lacking a description is a build
failure.

## Implementation surface

```
backend/src/agent/
  mod.rs                       # registers the executor + primitive registry
  primitives/
    mod.rs                     # registry, dispatch, common types
    wallet_profile.rs
    neighborhood.rs
    community_members.rs
    path_between.rs
    top_by_metric.rs
    time_window_diff.rs        # warehouse-bound
    tag_lookup.rs
  types.rs                     # ProvenanceRef, PageCursor, errors
  tool_descriptions.rs         # ts-rs export of tool schemas

frontend/src/lib/generated/    # auto-generated TS types
  ProvenanceRef.ts
  WalletProfile.ts
  Neighborhood.ts
  ...
```

ClickHouse:
- `CREATE USER agent_reader ... PROFILE 'readonly'` with explicit
  `max_execution_time = 10` and `max_rows_to_read` per query.
- `GRANT SELECT ON multichain.* TO agent_reader`.
- No `INSERT`, `ALTER`, `DROP`, `CREATE` grants.

## Verification

Each primitive ships with:
- Unit tests against in-memory `GraphState` fixtures (pattern matches
  `backend/src/analytics/*::tests`).
- For warehouse primitives, an integration test that runs against a
  test ClickHouse instance (or a docker-compose service for CI).
- A test that asserts the `Output` round-trips through `serde_json`
  without loss (the wire path).

End-to-end smoke test for the phase: a Rust integration test calls
the executor with a fabricated `(operation_name, arguments_json)`
pair for each primitive, asserts schema-valid output, and checks
that one provenance ref per output is well-formed.

## NOT in this phase

- Agent loop, prompt assembly, tool selection (phase 03).
- Cost gating, budget decrement (phase 05).
- Action ledger writes (phase 04).
- ClickHouse query tuning beyond `max_execution_time`. Optimize only
  if phase 06 surfaces hot primitives.

## Resume prompt for chat

> Phase 02 (typed primitive layer). Start from
> `docs/agent-design/02-typed-primitive-layer.md`.
> Resolve open questions 1-5, then implement the seven primitives
> with the cost-class metadata and provenance refs as designed. No
> agent loop yet, no budget gating yet.
