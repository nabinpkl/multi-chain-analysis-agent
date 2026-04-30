# 05: Anonymous principal model and cost-as-rate-limit

How the system identifies a "principal" without accounts or
fingerprinting, and how it enforces per-principal spend ceilings
across multiple cost dimensions instead of the conventional
requests-per-second cap.

## Problem

Two coupled problems:

1. **Identifying anonymous traffic.** No accounts, no auth. Every
   incoming request is from a stranger. The system still needs a
   stable bucket to attribute behavior and enforce limits against.

2. **Rate-limiting an LLM agent.** Conventional RPS caps don't model
   what actually costs money or compute here. One agent session can
   issue ten requests-per-second of cheap tool calls and cost
   nothing, while a single carefully-chosen `time_window_diff` query
   pegs a CPU for ten seconds. The unit of pressure is dollars per
   hour (LLM tokens), milliseconds per hour (DB time), and tool
   invocations per minute (loop detection); not requests.

Both problems have to be solved together because the principal model
is what the budget buckets key on.

## Industry standards

### Anonymous identity without fingerprinting

- **Server-issued opaque session tokens.** Cookie set on first
  request, random unguessable id, server holds the bucket. Standard
  pattern for free-tier API access, anonymous shopping carts, etc.
  Stripe's anonymous-checkout flow, OpenAI's pre-login playground,
  GitHub's unauthenticated API all use variants.
- **IP-based bucketing with truncation.** GDPR-respectful pattern
  documented in EDPB Guidelines 04/2019. Truncate IPv4 to /24 and
  IPv6 to /56 before storing. Yields a coarse principal suitable for
  rate limiting without identifying a household.
- **Cloudflare Turnstile.** Privacy-respecting CAPTCHA replacement.
  Single-shot bot/human signal without fingerprinting. The right
  answer for "is this a real browser" when account creation is not
  on the table.
- **Browser fingerprinting (rejected).** Documented in EFF's
  Panopticlick research (2010 onward) as a privacy risk.
  Fingerprinting libraries (FingerprintJS, etc.) collect canvas,
  font, WebGL, audio, screen, timezone signals. Even hashed, the
  derived id is a tracking identifier subject to GDPR consent.
  Excluded by design.

### Cost-as-rate-limit

- **Token bucket (Turner, 1985 / Cisco usage).** The data structure.
  Capacity + refill rate, request admitted if tokens available.
  Repurposed here so the "tokens" represent cost units, not request
  counts.
- **Vendor API rate-limit shapes.** Major providers (OpenAI,
  Anthropic, Google, etc.) all converge on per-axis buckets:
  separate counters for input tokens, output tokens, requests per
  minute, enforced simultaneously. Multi-axis is the industry-
  default at scale; we mirror the shape internally for our own
  budget framework.
- **Provider-side organization spend limits.** Per-org dollar
  ceilings per month, hard-capped at the vendor level. A coarser
  version of the same idea.
- **ClickHouse `max_execution_time` and `max_rows_to_read`.** The
  database-side cost ceiling. Per-query and per-user. Set as
  defense-in-depth alongside the application-level budget.
- **ClickHouse `EXPLAIN ESTIMATE`.** Returns estimated rows / bytes
  read for a query plan without executing it. The standard pre-flight
  cost-gating primitive in ClickHouse-native systems. Reference:
  ClickHouse documentation, `EXPLAIN ESTIMATE`.
- **Snowflake credit budgets.** Adjacent vendor pattern: per-warehouse
  credit ceiling, query-level credit estimate available at submission.
  Same shape as ClickHouse + EXPLAIN; mentioned because the resume
  pattern transfers cleanly.

## Open questions

1. **Where buckets live.** In-process for v0 (single-instance
   deployment, no need for shared state). Redis if multi-instance.
   Default position: in-process; document the seam to swap to Redis
   in this file.

2. **Refill cadence.** Per-second drip, hourly window, or sliding
   window? Token-bucket classical drip is per-second; sliding window
   is more precise but more state. Default: per-hour budget windows
   with continuous drip refill (capacity / 3600 per second).

3. **Bucket sizing.** What's the actual budget per anonymous
   principal? Needs to accommodate one substantial analysis and
   reject sustained abuse. Working numbers for v0:
   - LLM tokens: 100k input + 30k output per hour
   - DB time: 60 seconds per hour
   - Tool calls: 200 per hour
   - Concurrent sessions: 2 per principal
   These are starting points; tune from observed usage in phase 07.

4. **Budget exhaustion behavior.** Hard-stop the agent? Let the
   current claim finish then stop? Tell the user "you have N% left,
   stopping in M seconds"? Default: graceful stop after current
   claim completes; user-facing message includes remaining-budget
   info.

5. **What constitutes "the same principal"?** Cookie alone, IP
   alone, or both? Default: cookie-AND-IP combined into a hash. A
   request matches a principal only if both axes match. New cookie
   from same IP is a new principal but the per-IP secondary bucket
   still applies.

6. **Cookie expiry.** Session cookies (browser-lifetime) or
   long-lived (e.g. 30 days)? Long-lived is more user-friendly (the
   bucket state persists across browser restarts) but feels more
   tracking-flavored. Default: session-only; principals are
   ephemeral.

7. **Budget visibility to the agent.** The agent receives
   "you have 64% of your token budget remaining" in a dynamic system
   prompt section. Refresh on every loop iteration or just at
   session start? Default: every iteration; the agent's cost-aware
   adaptation depends on current state.

## Approach

### Principal construction

```
session_id = cookie["agent_session"] or generate_new_random()
ip_bucket  = sha256(client_ip_truncated_to_/24 or /56)[..16]
principal  = sha256(session_id || ip_bucket)[..16]
```

`principal` is the key for all per-principal buckets. `ip_bucket` is
the secondary bucket key (same IP, different cookie -> still
constrained on the IP axis). `session_id` rotates if the cookie
changes; `ip_bucket` rotates if the user moves networks. Both
rotating defeats the limit; rotating either alone does not.

The cookie is `Secure`, `HttpOnly`, `SameSite=Lax`. Lifetime per
open question 6.

### Multi-axis buckets

Four buckets per principal:

```rust
pub struct PrincipalBuckets {
    pub tokens:     TokenBucket,   // input + output, weighted
    pub db_time_ms: TokenBucket,   // ClickHouse query milliseconds
    pub tool_calls: TokenBucket,   // count, catches loops
    pub sessions:   SemaphoreCount, // concurrent active agents
}
```

Plus one per-`ip_bucket` secondary set with the same axes but a
larger ceiling (allowing multiple legitimate users behind a NAT
while still bounding abuse from a single /24).

A primitive call passes if:
1. The principal's `tool_calls` bucket has >= 1 unit.
2. The relevant cost-axis bucket (tokens for LLM, db_time_ms for
   warehouse, etc.) has >= the pre-flight estimate.
3. The IP secondary bucket of the same axis also has headroom.

First failing bucket short-circuits. The error returned to the agent
distinguishes which bucket and how long until refill so the agent
can decide between waiting and stopping.

### Pre-flight cost estimation

Per cost axis:

**LLM tokens (stochastic):** pessimistic reservation pattern. Reserve
`input_tokens + max_output_tokens` from the bucket before the call.
After the call returns, refund unused output budget
(`max_output_tokens - actual_output_tokens`). Net effect: an
LLM call can never overdraw, but the reservation is an upper bound,
so legitimate calls see lower throughput than the worst case.

**Database time (deterministic with EXPLAIN):** for warehouse
primitives, run `EXPLAIN ESTIMATE` against the parameterized SQL
with the supplied arguments. The result includes estimated rows
read; convert to ms via a calibration constant
(`ESTIMATED_MS_PER_ROW`, derived from a benchmark, recalibrated
quarterly). Refuse if estimate exceeds remaining `db_time_ms`
budget. After execution, decrement by actual `query_duration_ms`
from `system.query_log`. Drift is logged.

**Live primitives (declared cost class):**
- cheap = 1 unit
- moderate = 5 units
- expensive = 20 units
The unit corresponds to the `tool_calls` bucket. No DB time, no
tokens, just a fixed cost class.

**Tool calls (count):** every primitive invocation decrements 1 from
the `tool_calls` bucket regardless of axis. Catches the agent-stuck-
in-a-loop case where every individual call is cheap but cumulative
calls are pathological.

### Cost-aware behavior contract

The agent's dynamic system prompt section includes:

```
You have the following budget remaining for this session:
- Tokens: 64% (32k of 50k remaining for the next hour)
- DB time: 88%
- Tool calls: 73% (146 of 200)

When budget is below 30% in any axis, prefer cheap primitives
and concise outputs. When below 10%, summarize what you have and
stop.
```

This is supplied at every loop iteration. The behavior is
emergent from the prompt, not enforced at the runtime. Phase 06's
eval suite includes "agent adapts when budget is low" as a
golden test.

### Hard-stop semantics

When a bucket is empty AND the agent issues a call that would
require it:

1. The runtime returns a `BudgetExhausted` error to the agent's
   tool-call return.
2. The agent receives the error in the next turn's tool result.
3. If the agent emits another tool call after `BudgetExhausted`,
   the runtime hard-stops the session (this is the loop-detection
   guarantee).
4. Either way, the session emits a final `Done` event with
   `reason = budget_exhausted` and a summary.

### Defense-in-depth on the database

ClickHouse-side limits as a backstop:

```sql
ALTER USER agent_reader SETTINGS
    max_execution_time         = 10,        -- seconds
    max_rows_to_read           = 50_000_000,
    max_memory_usage           = 1_000_000_000,
    readonly                   = 1;
```

Even if the application-level budget logic has a bug, the database
will refuse pathological queries. Belt and suspenders.

### Bucket persistence

In-process v0:

```rust
pub struct BucketStore {
    principals: dashmap::DashMap<PrincipalKey, PrincipalBuckets>,
    ip_buckets: dashmap::DashMap<IpKey, IpBuckets>,
    last_drip_ms: AtomicU64,
}
```

Drip refill happens lazily: `acquire(N)` reads `last_drip_ms`,
computes elapsed seconds, adds `(elapsed * refill_per_second)` to
the bucket up to capacity, then attempts the acquire. No background
task needed.

Restart semantics: in-process buckets clear on process restart.
Acceptable for v0; Redis migration (open question 1) preserves
across restarts. The seam is the `BucketStore` trait; swapping the
backing store is a localized change.

### Telemetry

Every bucket decrement writes a `BudgetDecrement` event to the
ledger (phase 04). The event includes:
- Bucket axis (`tokens` / `db_time_ms` / `tool_calls`)
- `pre_estimate_units` (what was reserved)
- `post_actual_units` (what was consumed; for refund cases, the
  reserved amount minus the refund)
- The associated principal hash + session id

Phase 04's drift query then surfaces estimator quality per axis.

## Implementation surface

```
backend/src/agent/
  principal/
    mod.rs               # PrincipalKey construction, IP truncation
    cookie.rs            # session cookie issuance, Secure/HttpOnly
    middleware.rs        # tower / axum middleware to attach
                         #   principal to request extensions
  budget/
    mod.rs               # BucketStore trait + in-process impl
    bucket.rs            # TokenBucket, drip refill
    estimate.rs          # pre-flight estimators per cost axis
    explain.rs           # ClickHouse EXPLAIN ESTIMATE wrapper
    errors.rs            # BudgetError variants
  runtime.rs             # ties primitive executor to budget gate

migrations/
  0007_agent_reader_limits.sql   # ClickHouse user-level limits
```

Frontend:
- Send `credentials: "include"` on the agent SSE request so the
  cookie travels.
- Surface remaining-budget info from the agent's `Done` event in the
  sidebar UI footer.

## Verification

- Two browsers from the same machine: each gets a distinct
  `session_id` cookie, distinct principal hash, separate buckets.
  Confirm via the ledger.
- Same browser, clear cookies, reload: new principal hash; per-IP
  bucket carries over (verify the secondary bucket decremented from
  the previous session is still reduced).
- Submit a known-pathological query
  (`time_window_diff` with a 30-day range): refused at EXPLAIN
  pre-flight; budget did not decrement.
- Run an LLM call with `max_tokens=4000`, observe actual output of
  500 tokens: ledger shows reservation of 4000, refund of 3500.
- Drain the `tool_calls` bucket via repeated cheap calls: subsequent
  calls return `BudgetExhausted` before the LLM is called.
- ClickHouse-level: send a query that the application bypassed
  somehow (manually), with execution > 10s; ClickHouse refuses on
  `max_execution_time`.

## NOT in this phase

- Redis migration (deferred to multi-instance deployment).
- Per-principal long-term reputation (treating returning principals
  with more leniency or stricter caps based on history). Out of
  scope for v0; the seam exists in the `BucketStore` trait but the
  default impl ignores history.
- Stripe-style spend dashboards. Phase 07 if useful.
- Cloudflare Turnstile integration. Add only if abuse appears.

## Resume prompt for chat

> Phase 05 (anonymous principal + cost rate-limiting). Start from
> `architecture-decisions/chain-analysis-agent/05-anonymous-principal-and-cost-rate-limiting.md`.
> Resolve open questions 1-7, then implement principal construction,
> the four buckets, pre-flight estimation per axis, the EXPLAIN
> wrapper, ClickHouse-side defense-in-depth limits, and ledger
> writes for every decrement. Phase 04 must be in place.
