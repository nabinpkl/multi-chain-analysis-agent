# 07: Polish and analyst surfaces

The phase that turns the agent from "works correctly" into "useful
during analytical work". Surfaces that expose the underlying
machinery (ledger, cost, drift) to a human, and integrations that
extend the agent's reach to external data sources.

## Problem

The first six phases produce a correct, defended, instrumented
agent that emits provenance-attached claims to a sidebar. That is
the minimum viable product. The polish phase addresses the gap
between "minimum" and "useful":

1. **Session traceability is currently programmatic only.** A user
   reporting a bad answer has no UI for inspecting what the agent
   did. Engineers reach into ClickHouse manually.

2. **Cost is invisible in the UI.** The user has no signal that
   their session has a budget until they hit the limit. A
   pre-emptive indicator would make budget-aware behavior coherent.

3. **Drift telemetry sits in tables.** Without a visualization, the
   estimator decay surfaced by phase 04 doesn't drive recalibration
   in practice.

4. **External tags don't flow yet.** The `tag_lookup` primitive
   (phase 02) returns only internal labels. Real analytical value
   appears when the agent can say "this wallet is the Binance hot
   wallet" or "this address is a known Jito tipper". External
   sources are the lever for that.

5. **Documentation is internal-only.** The architecture-decision
   docs (this folder) describe the system to maintainers. A surface
   description for external readers (a README, a public-facing
   architecture page, a series of write-ups) does not exist.

Polish is not "nice to haves"; each item makes the system meaningfully
more useful or more legible. They land in priority order, and any
subset can ship.

## Industry standards

- **Datadog / Honeycomb / Grafana style observability dashboards.**
  Patterns for surfacing cost and latency over time. Time-series
  panels, percentile aggregations, drill-down to individual events.
- **OpenTelemetry trace viewers (Jaeger UI, Tempo).** The
  span-with-children visualization for agent traces. Each tool call
  is a span; nested LLM calls are child spans; the whole session is
  the root span. Adopt the visual idiom even though we don't use
  OpenTelemetry directly (yet).
- **Blockchain wallet tag aggregators (Helius, Solscan, Dune).** The
  prior art for integrating external Solana labels. Each has an API
  with rate limits, a label schema, and TOS implications. Reference:
  Helius API documentation, Solscan API.
- **Jito's published tip-account list.** A self-published, free,
  trustworthy source of one specific label class. Lowest-friction
  external integration.
- **Engineering writing conventions: ADR (Architecture Decision
  Record) format, RFC-style docs.** The shape this folder already
  uses. Public-facing write-ups extend the same shape to a wider
  audience.

## Open questions

1. **Which polish items ship.** This is a menu, not a checklist.
   Each item has independent value. Ship items in observed-need
   order: a user reporting a bad claim is the trigger for the
   trace viewer, observed cost surprises trigger the dashboard,
   etc.

2. **External tag-source integration scope.** Helius alone, or
   multiple? Helius has comprehensive labels with a free tier;
   Solscan is broader but rate-limited. Default position: Helius
   first, additive from there. Tag attribution per-source so a
   claim citing a tag can show "from helius.xyz".

3. **Tag-source caching strategy.** External APIs have rate limits
   and latency. Cache labels in ClickHouse with a TTL? In-memory
   with periodic refresh? Default: ClickHouse-backed, day-level
   TTL, refreshed lazily on access.

4. **Public-facing documentation.** Internal ADRs (this folder)
   are the source of truth for design. The public-facing writeup
   draws from them but rephrases for an external reader. Keep
   them in sync mechanically (a script that lints for drift) or
   manually (review pass). Default: manual; revisit if drift
   becomes a problem.

5. **Polish-phase budget.** This phase has no clean "done"
   condition. Set a time-box (e.g. one to two weeks) and ship
   what fits.

## Approach

The polish menu, prioritized:

### P-1: Session trace viewer

A frontend page or sidebar tab keyed by session id. Renders the
ledger (phase 04) as a timeline:

```
[session-uuid]
 t=0ms        SessionStarted (principal abc123)
 t=12ms       Prompt (system v3, user "profile wallet 9n4...")
 t=140ms     LlmCall (model: <pinned>, 1240 input)
 t=2890ms     LlmResponse (450 output, stop=tool_use)
 t=2891ms      ToolCall wallet_profile { addr: "9n4..." }
 t=2912ms      ToolResult (12 KB) ok
 t=2913ms     LlmCall ...
 t=4800ms     ClaimEmitted profile-001 (Approved)
 t=4801ms    SessionEnded reason=normal cost=8.4k tokens
```

Click any event to expand the payload (after redaction policy from
phase 04 applies). The viewer is the single source of truth for "what
did the agent actually do".

Implementation surface:
- `frontend/src/app/sessions/[id]/page.tsx`
- `frontend/src/components/agent/session-trace.tsx`
- `backend/src/api/agent_session.rs` (GET /agent/session/:id)
- Auth: viewer is currently open since data is public; gate behind
  a passphrase if abuse appears.

### P-2: Cost dashboard and drift panel

A page showing aggregate behavior across all sessions:

- Tokens spent, DB time consumed, sessions started over time
  (24h / 7d / 30d).
- Per-primitive average cost.
- Drift mean and p95 per cost class.
- Top sessions by cost (clickable into the trace viewer P-1).
- Top principals by spend (hashed; not user-identifying).

Source: ClickHouse queries against the `agent_ledger` table (phase
04). Refresh every 30 seconds. The frontend is read-only.

Implementation surface:
- `frontend/src/app/admin/cost-dashboard/page.tsx`
- `backend/src/api/agent_metrics.rs` (GET /agent/metrics/...)
- ClickHouse materialized views for hot panels (avoid re-aggregating
  the entire ledger on every panel refresh).

### P-3: User-facing budget indicator

In the agent sidebar, surface remaining budget in a small footer:
"43% of your hourly budget remaining". Refreshes after each claim.
Same data the agent itself sees in its system prompt; visible to the
user for transparency.

When budget is below 10%, the sidebar shows a banner: "you have
limited budget remaining; the agent will summarize and stop soon".

Implementation surface:
- Extend the existing `Done` event in the SSE stream (phase 03) to
  include a remaining-budget snapshot.
- `frontend/src/components/agent/budget-footer.tsx`

### P-4: External tag database

Integrate Helius (and optionally other sources). The `tag_lookup`
primitive (phase 02) gains a multi-source backend.

Schema:

```sql
CREATE TABLE wallet_tags (
    addr             String,
    source           LowCardinality(String),
    tag              String,
    confidence       Float32,
    fetched_at       DateTime,
    expires_at       DateTime,
    raw_payload      String   -- the source's full record
)
ENGINE = ReplacingMergeTree(fetched_at)
PARTITION BY toYYYYMM(fetched_at)
ORDER BY (addr, source, tag);
```

The primitive checks the cache, returns hits immediately. Misses
trigger an async fetch from the source's API; the primitive returns
"unknown for now" and the cache populates for next time. No
synchronous external API calls during the agent loop (would blow
through DB-time budgets and add unbounded latency).

Source attribution flows through provenance: a claim citing a tag
includes the source in the `ProvenanceRef`, and the UI renders
"per helius.xyz" next to it.

Critically, this is when the **layer-1 untrusted-text defense**
(phase 03) starts mattering for real. The tag content comes from a
third party; the `<external_data>` wrapper plus the role-based
boundary protects the agent's reasoning. v0 was prepared for this;
v1 is when it gets exercised.

Implementation surface:
- `backend/src/agent/primitives/tag_lookup.rs` extended for
  multi-source.
- `backend/src/agent/tags/helius.rs` (and per-source files).
- `backend/src/agent/tags/cache.rs` (ClickHouse-backed cache).
- Background tokio task that pre-warms tags for active wallets in
  the live graph.

Open: rate limits per source. Helius has a generous free tier;
respect it. Backoff strategy on 429; surface to operators via the
cost dashboard P-2.

### P-5: Public-facing documentation

The architecture-decision folder is the internal source of truth.
Public-facing surfaces draw from it for an external reader who
wants to understand or evaluate the system:

- A `README.md` at the repo root that pitches the system in one
  page, links to a tour of the architecture.
- A `docs/` page tour of each layer with links into source code.
- Short engineering write-ups (one per layer that has a
  generalizable insight): cost-as-rate-limit, three-layer prompt
  injection defense, provenance-attached claims.

Implementation surface:
- `README.md` (root)
- `docs/architecture/agent.md`
- `docs/posts/cost-as-rate-limit.md`
- `docs/posts/prompt-injection-defense.md`
- `docs/posts/agent-evaluation.md`

The write-ups are general-engineering, not project-specific. They
reference our system as a case study. The pattern is the artifact;
the system is the demonstration.

### P-6: Multi-turn conversation

Phase 03 deferred multi-turn (open question 4 in that file).
Polish phase adds it: a session retains prior claims and accepts
follow-up questions ("expand on the second wallet", "compare those
to the previous hour").

Implementation:
- Session state lives in the action ledger (phase 04). A follow-up
  loads the prior claims as context.
- Budget continues across turns (the principal bucket is unchanged;
  the session has accumulated cost).
- The system prompt includes a "previous claims in this session"
  section listing prior `Claim` ids and headlines. The agent can
  reference them; the UI highlights cross-references.

Implementation surface:
- `backend/src/agent/loop.rs` extended with a multi-turn driver.
- `frontend/src/components/agent/agent-sidebar.tsx` extended with
  a turn-by-turn UI.

### P-7: Cloudflare Turnstile gate

Add only if abuse appears. Single-shot bot/human signal in front of
the agent endpoint. No fingerprinting. Preserves the anonymous-user
model from phase 05.

Implementation surface:
- `frontend/src/components/agent/turnstile-gate.tsx`
- `backend/src/agent/turnstile.rs` (server-side verification)

## Implementation surface

(varies by item; each is self-contained)

## Verification

Polish items each have their own success criteria:

- P-1 trace viewer: paste a session id, see the timeline, click
  events, payloads render.
- P-2 cost dashboard: panels show non-empty data within 30 seconds
  of agent activity.
- P-3 budget indicator: refreshes per claim; banner appears below
  10%.
- P-4 tag database: a known tagged wallet (e.g. a Jito tipper)
  surfaces with a tag in the next agent profile call.
- P-5 docs: a colleague with no context reads the README and can
  describe the system back accurately.
- P-6 multi-turn: ask "profile X", then "what about its top
  counterparty"; agent recognizes the reference.
- P-7 Turnstile: legitimate users complete the challenge once per
  session; bots don't pass.

## NOT in this phase

- A full SaaS administration UI (user management, billing,
  multi-tenant). The skip list is in
  `01-agent-overview.md`; polish does not add it back.
- Live mutations (any "do X on chain") capability. The agent
  remains read-only forever.
- Conversational long-term memory across sessions for a returning
  principal. Out of scope for v0; the principal model is ephemeral
  by design.

## Resume prompt for chat

> Phase 07 (polish + analyst surfaces). Start from
> `architecture-decisions/chain-analysis-agent/07-polish-and-analyst-surfaces.md`.
> Pick which P-1 through P-7 items ship in what order. Phase 06
> must be in place; specific items have additional dependencies
> (e.g. P-1 needs phase 04 ledger).
