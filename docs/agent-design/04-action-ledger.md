# 04: Action ledger

> **SUPERSEDED by ADR 14 (Agent observability foundation).** This
> phase doc planned a bespoke append-only ledger as the per-step
> record. ADR 14 replaces it with OpenTelemetry spans (Pydantic AI
> built-ins + custom domain spans) as the single source of truth.
> The `agent_ledger` table and module are deleted in Ship 1 of the
> observability foundation. Kept here as historical record of the
> original design; current design lives at
> `architecture-decisions/14-agent-observability.md`.

Append-only structured record of every prompt, tool call, tool
result, claim emission, and policy verdict the agent produces.
Foundational data shape that the cost-rate-limit (phase 05) and
evaluation (phase 06) layers both read from.

## Problem

Without a ledger, three classes of question become unanswerable in
practice:

1. **What did the agent actually do?** A user reports a wrong claim.
   No record means no diagnosis. The model output alone is
   insufficient because it omits the tool-result context the model
   reasoned over.

2. **Is the agent regressing?** Without per-call cost and accuracy
   data over time, model upgrades, prompt changes, and primitive
   refactors land blind. The eval suite (phase 06) can run a fixed
   question set, but only the ledger turns each session into a
   replayable artifact.

3. **What is the agent costing?** Aggregate billing dashboards show
   spend but not attribution. The ledger is what lets us answer
   "which primitive is expensive on average" or "this session burned
   $X, why".

The ledger answers all three by capturing, per session, every input
and output along the agent's path with enough metadata to reconstruct
the behavior offline.

## Industry standards

- **OpenTelemetry semantic conventions for LLM systems.** The current
  industry trajectory for instrumenting LLM applications. Defines
  span shapes for `gen_ai.request`, `gen_ai.response`,
  `gen_ai.tool.call`, etc. Reference: OpenTelemetry GenAI semantic
  conventions, current draft.
- **Audit logging in regulated systems.** SOC2 Common Criteria
  CC7.2, HIPAA 164.312(b), PCI DSS 10. Each requires append-only,
  tamper-evident logs of system actions. Same shape applies here
  even though the data is public: the discipline is what matters.
- **Event sourcing.** Pattern from domain-driven design (Evans,
  Vernon, Fowler). The system's state is a fold over an append-only
  event stream. Ledger entries are events; agent session state is
  derivable by replay.
- **Append-only with content addressing.** Patterns from systems like
  Git, Merkle DAGs, write-ahead logs. The ledger is append-only at
  the schema level (no `UPDATE`, no `DELETE`); content hashing of
  prompts and responses lets us detect tampering even though
  ClickHouse itself is mutable.
- **ClickHouse `MergeTree` engines.** The natural store for
  high-write append-only telemetry. `MergeTree` partitioned by day
  with `ORDER BY (session_id, sequence)` gives cheap session
  reconstruction queries.

## Open questions

1. **Storage engine choice.** ClickHouse is the obvious answer
   (already running, tuned for append-only writes). The alternative
   is a separate Postgres instance for stronger transactional
   guarantees. Default position: ClickHouse for v0; reconsider if
   the eval suite (phase 06) needs strong read-your-writes.

2. **Retention policy.** How long do we keep ledger entries? Phase
   06 needs at least the duration of the longest eval comparison
   window. Practical floor: 90 days. Practical ceiling: indefinitely
   (it's small data). Default: 90 days, partitioned by day,
   ClickHouse TTL clause drops older data automatically.

3. **What goes into a `Prompt` event?** Full prompt text, or hash +
   version tag? Storing full text means a leaked ledger is a prompt
   leak. Storing hash + version means replays need the original
   prompt source code at the same version, which means tagging
   prompt versions and storing them in the repo. Default: hash +
   version tag, prompts versioned in source. Adopt full-text only if
   debug needs justify the cost.

4. **Tool result content.** Tool results can be large
   (`neighborhood(depth=2)` returns an entire subgraph). Inline in
   the ledger row, store separately, or store a content hash with
   the full payload elsewhere? Default: inline up to N kilobytes
   (e.g. 64 KiB), spillover to a content-addressed blob with the
   hash in the ledger.

5. **Cost telemetry slot semantics.** `pre_estimate` and
   `post_actual` fields exist on every event. For events that
   neither estimate nor actually cost anything (e.g. a `Prompt`
   record), what value goes there? Default: `0` for both with a
   separate `cost_relevant: bool` to distinguish from "did not
   measure". Avoids null semantics in the analysis queries.

6. **PII in the ledger.** Per the threat model none exists. But the
   ledger itself becomes the surface where we'd add PII redaction
   later. Define the seam now: a `redaction_policy_version: u32`
   field on every event, defaulting to a no-op policy in v0. Adding
   redaction in a later phase becomes a policy version bump, not a
   schema migration.

## Approach

Every ledger entry is one row in a single ClickHouse table. The row
shape uses a discriminated event type with a JSON payload. This is
the industry-standard event-sourcing pattern adapted to a columnar
store.

### Event types

```rust
pub enum LedgerEventKind {
    SessionStarted,             // session_id, principal hash, timestamp
    Prompt,                     // version tag, hash, role
    LlmCall,                    // model id, input + output token counts
    LlmResponse,                // stop reason, content hash
    ToolCall,                   // primitive name, args hash
    ToolResult,                 // content hash, byte size, error if any
    ClaimEmitted,               // claim id, kind, policy verdict
    PolicyVerdict,              // claim id, verdict, reason
    BudgetDecrement,            // bucket name, units, pre_estimate
                                //   vs post_actual
    SessionEnded,               // reason, summary stats
}
```

### Schema

```sql
CREATE TABLE agent_ledger (
    session_id            UUID,
    sequence              UInt64,           -- monotonic per session
    timestamp_ms          UInt64,
    kind                  LowCardinality(String),
    principal_hash        FixedString(32),  -- SHA-256 of session
                                            --   cookie + truncated IP
    payload               String,           -- JSON, schema per kind
    payload_hash          FixedString(32),  -- SHA-256 of payload
    pre_estimate_units    UInt32,
    post_actual_units     UInt32,
    cost_relevant         UInt8,            -- 0/1
    redaction_policy_ver  UInt32,
    inserted_at           DateTime DEFAULT now()
)
ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(toDateTime(timestamp_ms / 1000))
ORDER BY (session_id, sequence)
TTL toDateTime(timestamp_ms / 1000) + INTERVAL 90 DAY
SETTINGS index_granularity = 8192;
```

`ORDER BY (session_id, sequence)` is what makes session replay a
single-partition scan. `TTL` enforces retention without operational
work.

### Write path

The agent's runtime exposes a `Ledger` service injected into the
loop, the policy pass, and the budget layer. Every action that
fits an event kind calls `ledger.write(event)`. Writes are async,
batched, and acknowledged before the agent's next tool call so a
crash mid-session leaves at most a few unwritten events (which is
acceptable; the session is unrecoverable anyway).

The write path is one-way: no `UPDATE` or `DELETE`. ClickHouse
permits these in principle, but the read-only role used by the
agent's analytical reads (phase 02) does not include them. The
write role used by the ledger writer is a separate principal.

### Replay path

```rust
pub fn replay_session(session_id: SessionId) -> Vec<LedgerEvent>;
pub fn render_session(session_id: SessionId) -> SessionTrace;
```

`replay_session` returns the events in sequence order. `render_session`
folds the events into a structured trace: turn-by-turn agent
behavior, every tool call with its result, every claim with its
policy verdict, total cost. The fold is deterministic given the
event stream.

The render is the basis for:
- Debugging individual sessions when a user reports a bad claim.
- Eval suite regression comparison (phase 06): same question, two
  agent versions, render both, diff.
- The portfolio-grade session-trace viewer (phase 07).

### Content addressing

`payload_hash` is a SHA-256 of the canonicalized JSON payload.
`principal_hash` derives from the session cookie + truncated IP
(phase 05 owns the construction). These hashes serve two purposes:
- Tamper detection. If a prompt or tool result is modified
  out-of-band, the hash mismatch surfaces in replay.
- Cross-event linking without storing duplicate strings. A
  `LlmResponse`'s content hash matches the `Prompt` hash that
  produced it (in the next event by sequence), so chains of agent
  reasoning are traceable.

Spillover for large payloads (open question 4): payloads above 64
KiB are written to a `agent_ledger_blobs` table with `(payload_hash,
content)` and the ledger row stores the hash with empty `payload`.
Replay rejoins on hash.

### Drift telemetry

Every cost-relevant event carries `pre_estimate_units` and
`post_actual_units`. The drift (`actual - estimate`) is its own
metric:

```sql
SELECT
    kind,
    avg(post_actual_units - pre_estimate_units) AS mean_drift,
    quantile(0.95)(post_actual_units - pre_estimate_units) AS p95_drift,
    count()
FROM agent_ledger
WHERE cost_relevant = 1
  AND timestamp_ms > now() - INTERVAL 7 DAY
GROUP BY kind
ORDER BY mean_drift DESC;
```

A primitive whose mean drift becomes consistently positive means the
estimator is wrong; budget over-allocates. Consistently negative
means estimator is conservative; legitimate work gets blocked.
Phase 06's eval suite asserts drift stays within a band.

## Implementation surface

```
backend/src/agent/
  ledger/
    mod.rs               # Ledger service, async batch writer
    event.rs             # LedgerEvent + LedgerEventKind enums
    write.rs             # ClickHouse INSERT path, batching
    replay.rs            # session reconstruction
    blob.rs              # large payload spillover
  prompt_versions.rs     # const tags for each system prompt rev

migrations/
  0006_agent_ledger.sql  # ClickHouse DDL for both tables
```

ClickHouse user setup:
- `CREATE USER agent_ledger_writer ...` with INSERT-only on
  `agent_ledger` and `agent_ledger_blobs`.
- Existing `agent_reader` role from phase 02 cannot write.

## Verification

- A full agent session run end-to-end leaves a complete ledger.
  Replay reconstructs every prompt, tool call, claim, verdict in
  order.
- Schema check: a deliberate `UPDATE` against the ledger table fails
  on the writer principal.
- Hash check: corrupt a payload column out of band; replay surfaces
  a hash mismatch.
- TTL check: insert a row with a 91-day-old timestamp; ClickHouse
  drops it on next merge.
- Drift check: run the drift query against a session known to have a
  budget mis-estimate; the offending kind ranks first.

## NOT in this phase

- Cost calculation logic that fills `pre_estimate_units` /
  `post_actual_units`. Phase 05 owns that. The fields exist but are
  zero in this phase.
- Eval suite that consumes the replay path. Phase 06.
- UI surface for the ledger (session-trace viewer). Phase 07.
- Tamper-evident chaining (Merkle-tree-style hashes linking each
  event to the previous). Out of scope; the per-event content hash
  is sufficient for v0. Add if a future phase needs cross-row
  tamper detection.

## Resume prompt for chat

> Phase 04 (action ledger). Start from
> `docs/agent-design/04-action-ledger.md`.
> Resolve open questions 1-6, then implement the schema, the writer
> service, the replay function, and the blob spillover. Wire the
> writer into the agent runtime so every loop iteration produces
> events. Cost fields stay zero until phase 05.
