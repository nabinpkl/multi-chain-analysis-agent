Your instinct is right. Fingerprinting buys you a lot but the privacy story gets ugly fast: even hashed canvas/font/WebGL signals are derived identifiers you didn't get consent for, and a hiring manager reading your code will notice. The defense it gives is also weak (any motivated attacker rotates).

The cleaner pattern, and the one most production rate-limiters use for anonymous traffic:

## Three layers, no fingerprint needed

**1. Edge rate limit (Cloudflare).** You already have Cloudflare in the path. Free tier gives you per-IP rate limit at the edge for gross volume defense. No client data stored on your side. Set a generous ceiling (per-IP requests per minute) just to absorb obvious abuse.

**2. Server-issued opaque session cookie.** First page visit, set a cookie with a random unguessable token. Server-side token bucket keyed by session id. User can clear cookies to get a fresh bucket, but that has a friction cost > 0, which is the whole point. Standard pattern for free-tier API access.

**3. Truncated-IP secondary bucket.** Per-IP bucket at `/24` (IPv4) or `/56` (IPv6) granularity. Defends against the "clear cookies, get new session, repeat" loop. Truncation is the privacy hygiene: you store enough to bucket abuse, not enough to identify a household. GDPR-friendly framing.

Lower-of-the-two-buckets wins. Evading both means rotating IP AND clearing cookies AND staying under the rate, which raises the bar enough that nobody bothers with a portfolio demo.

## The unit of measurement matters

Don't rate-limit by requests per second. Rate-limit by **dollar-equivalent budget per principal per hour**:

- LLM tokens consumed
- ClickHouse query-time consumed
- Tool calls (catches the agent-stuck-in-a-loop case)

Each session has a budget; agent decrements as it works; budget refills on a window. This is enterprise-shaped because real production systems care about spend, not RPS. "I metered cost, not requests" is a sharper talking point than "I rate-limited at 10 RPM".

## The bot/human signal

If you want to differentiate bot from human without fingerprinting, **Cloudflare Turnstile** is the right move. Privacy-respecting CAPTCHA replacement, no user-identifying data, fires once per session on first request. Solves "is this a real browser" without a fingerprint.

Yes. The bucket data structure stays. What changes is **what's in the bucket and what tops it up**. Classic RPS rate limit puts request counts in; cost rate limit puts dollars (or dollar-equivalent units). Token bucket, leaky bucket, sliding window — all of them work the same way, just with a different scalar.

The interesting design is multi-axis and timing-aware.

## Multi-axis buckets per principal

A real implementation has several buckets per principal, not one:

- **LLM token bucket**: input tokens + output tokens, weighted by model price
- **Database time bucket**: total ClickHouse query milliseconds
- **Tool-call count bucket**: catches agent-stuck-in-a-loop without needing to estimate cost per call
- **Concurrent sessions bucket**: prevents one principal from running 50 agents in parallel

A request is admitted only if **all relevant buckets have headroom**. First to drain blocks the request. Different attacks drain different buckets, so single-axis defense leaves gaps. Token loop drains the count bucket before the dollar bucket. Heavy ClickHouse query drains DB time before LLM tokens. Multi-axis catches both.

## Pre-flight estimate vs post-hoc actual

Cost is partly stochastic (LLM tokens) and partly deterministic (DB query plan). Two patterns, picked per action:

**Deterministic (ClickHouse):** run EXPLAIN before the query. ClickHouse returns estimated row scan, memory, etc. Compare against remaining bucket. Refuse if estimate exceeds budget. This is the killer move because it's actually unique. Most rate limiters just count after the fact. Pre-flight EXPLAIN-based gating means a cost-blowup query never runs at all. Saves the most money.

**Stochastic (LLM):** can't know token cost before the call. Pattern is pessimistic pre-flight reservation: reserve the max likely cost from the bucket, do the call, return the unused portion. So an LLM call with max_tokens=2000 reserves the 2000-token cost, even if it actually emits 400. User sees a worst-case bucket draw, gets a refund. Defends against many-small-but-massive-by-accident.

## Differential pricing per primitive

Not every primitive costs the same. `wallet_profile(addr)` is cheap. `find_rotation_rings(min_size=3, window=24h)` scans a lot. Each primitive declares its **expected cost class** and the bucket consumption reflects it. Real systems publish "this operation costs N credits" precisely so the user can plan.

This makes the agent itself cost-aware. The primitive descriptions in the catalog include cost class. The agent learns (via prompt, via observed bucket burn) that some tools are expensive and avoids them when budget is tight.

## Agent observing its own budget

Frontier move. The agent sees its remaining budget in the system context: "you have 60% of session budget remaining". It adapts strategy:

- High budget remaining → take a deep, exploratory path with multiple primitives
- Low budget → summarize what you have, stop, tell the user what's left undone

This crosses from rate limiting into **cost-aware autonomy**. Most agent demos don't do this. It's a strong portfolio paragraph: "the agent reasons about its budget the same way a careful analyst reasons about which queries to run before EOD".

## Estimate-vs-actual drift as a signal

Every cost-metered action logs both `pre_estimate` and `post_actual`. The bucket is decremented by actual; the estimate was for pre-flight gating. The **drift** is itself a metric:

- Drift consistently negative (estimate too low) → buggy estimator, real cost outpacing budget
- Drift consistently positive (estimate too high) → being too conservative, blocking legitimate work

Monitor the drift, alert on regressions. This is the kind of nuance senior engineers think about; most candidates skip it. "I have an instrumented cost estimator with drift telemetry" is a one-line resume bullet that signals you've actually done it.

## Observability falls out for free

Because every bucket decrement is logged with `(principal, primitive, estimate, actual, timestamp)`, you get a real-time spend dashboard:

- Which session burned the most
- Which primitives are pricey on average
- Which user pattern correlates with cost spikes
- Total spend today vs budget

End of day you can answer "where did the money go" with SQL on your own logs, not a Stripe dashboard surprise.

## What this becomes on the resume

"Cost-as-rate-limit: budget per principal in dollar-equivalent units, multi-axis buckets (LLM tokens / DB time / tool calls / concurrent sessions), pre-flight EXPLAIN-based gating for deterministic costs, pessimistic-reservation-with-refund for stochastic costs, differential pricing per primitive, agent reasons about its remaining budget, drift telemetry between estimate and actual."

That's roughly six things most engineers haven't combined into one system. Each piece is small; the combination is senior. And every piece transfers cleanly to enterprise: same design works against Snowflake credits or OpenAI org limits or vendor API quotas.

The phrase "saved $X by refusing queries that would have run away" is the kind of concrete number you can put in a portfolio post. Even if X is small in our context, the pattern is what gets recognized.

Want this folded into the agent plan, or any sub-mechanic to push on first? The two I'd flag for design before plan:

1. **The cost class taxonomy.** What are the cost tiers (cheap / moderate / expensive)? Pulled from where (declared in primitive metadata vs measured)?
2. **Where the bucket lives.** Redis? In-process? ClickHouse? Tradeoffs around persistence and multi-instance.