
# Must Do's
- Every backend feature change do docker compose up -d --build at last.

# Don'ts
- No God component if it makes sense to extract to a component do it
- No dead code — if something is removed, delete it entirely (files, imports, types, everything referencing it)
- No backward compatibility layers — this is iteration-based development; just change the code directly

# Writing rules (docs/LinkedInEngineeringPosts/ only)

These rules apply when drafting or editing post content inside `docs/EngineeringPosts/`.

- No em-dashes. They read as AI-written on sight. Use periods, commas, colons, or parens instead.
- No "X is not Y, it's Z" cadence unless it really earns it.
- Keep the numbers. They do the heavy lifting.
- First person, plain words, short paragraphs.
- Audience is peer engineers and technical hiring managers, not recruiters. Technical terms (O-notation, mmap, asymptotic) stay when they advance the story. Flex-for-flex's-sake (naming libraries just to sound senior) gets cut.
- The post is a log, not content marketing. Skip hook-bait openers. The reader arrived from a resume link, not a scroll.

# MultiChain Analysis Engine

# JustGetDomain.com — Build Context

## What It Is

Listen txs from multiple chain normalize it link each tx to the data then build a graph serve the graph.

## Infrastructure

**Oracle Free Tier VM:** 24GB RAM, 4 Ampere cores. Runs Rust binary + cloudflared. That's it.
- Next.JS Vercel deploy
**Security:** Zero open inbound ports. Cloudflare absorbs DDoS. Connection hygiene in Rust service.

## `frontend/` stack

- **Framework:** Next.js 16.2.3, App Router, TypeScript, `src/` directory, `@/*` alias
- **Package manager:** pnpm
- **Styling:** Tailwind CSS v4 (CSS-first via `@tailwindcss/postcss`)
- **UI components:** shadcn/ui — all components installed in `src/components/ui/`
- **State:** Zustand v5 (client), TanStack Query v5 (server)
- **Animation:** motion v12 (`motion/react`)
- Color: Every color should be oklch no # based colors (if needed convert first)

## Backend Rust Service Architecture

### Decision: Axum
Axum 0.8.x is the 2026 default. Built on Tokio, via Tower middleware for connection hygiene. Same runtime for batch (tokio::io file streaming) and serving. Single binary, single process.

### Core Crate Stack
#### All latest
tokio
axum
tower
serde
serde_json
rustc-hash         # FxHashSet — faster than std HashMap



**1. Idempotency on ingestion — non-negotiable.**

You will restart, replay, double-fetch. Make every write idempotent.

- Use `signature` (tx hash) as the primary uniqueness key
- ClickHouse: `ReplacingMergeTree(version)` engine on the edges table — duplicates collapse on merge
- Re-fetching slot N must produce the same DB state as fetching it once
- Why it matters: you'll absolutely run `getBlock(N)` twice during a restart. Without idempotency you have phantom edges in your graph.

**2. Durable checkpoint of last-processed slot.**

- Single-row table `ingestion_state` storing `(component, last_slot, updated_at)`
- Update **after** the batch commits, not before
- On startup, read it to know where to resume
- Why: without this, restart = re-ingest from genesis or skip slots silently. Both bad.

**3. Rate-limited RPC client wrapper.**

You have a hard 5 req/sec cap. Don't trust yourself to remember everywhere.

- Wrap the RPC client in a token-bucket limiter (`governor` crate)
- Set it to ~4/sec (leave headroom for bursts)
- Every call goes through the wrapper — no exceptions
- Why: violate the cap and you get 429s, then progressive backoff, then bans. Self-imposed limit prevents drift.

**4. Batched writes, not single-row inserts.**

ClickHouse hates 1-row inserts. Buffer in memory.

- Accumulate edges in a `Vec<Edge>` per ingestion worker
- Flush on either: 10k rows OR 5 seconds elapsed (whichever first)
- Use `tokio::sync::mpsc` channel between fetcher and writer
- Why: 1-row inserts can be 100× slower and cause part-fragmentation in ClickHouse.

**5. Graceful shutdown.**

`Ctrl+C` should not lose the current batch.

- `tokio::signal::ctrl_c()` triggers shutdown
- Drain in-flight RPC calls, flush write buffer, update checkpoint, then exit
- Why: ungraceful shutdown = re-ingestion of partial slots on next start, wasted RPC budget.

**6. Error categorization.**

Not all errors are equal. Decide once, encode in types:

| Error | Action |
|---|---|
| `-32007` skipped slot | Increment, continue (not an error) |
| `-32004` block unavailable | Wait, retry same slot (you're at tip) |
| 429 rate limit | Exponential backoff |
| 5xx / network | Retry with backoff, up to N attempts |
| Parse failure | Log + alert + skip slot (don't crash) |
| DB write failure | Retry batch, then crash if persistent |

Implement as `enum IngestError` with explicit handling in the loop.

**7. Schema migrations from day one.**

- `refinery` crate + numbered SQL files in `migrations/`
- Run on startup before opening connections
- Why: you *will* change the schema in week two. Doing it manually invites drift between dev/prod.

**8. Structured logging + tracing from the start.**

- `tracing` + `tracing-subscriber` (Rust ecosystem standard)
- Log: slot fetched, batch flushed, errors with context, rate limit hits
- JSON output in production, pretty in dev
- Why: when things break at 3am you need timestamps, slot numbers, and request IDs. Adding logs after the fact is painful.

**9. Frontend ↔ backend type contract.**

You have Rust backend + Next.js frontend. Don't hand-write TypeScript types.

- Generate TS types from Rust structs via `ts-rs` crate (derive macro)
- Or use `utoipa` to generate OpenAPI spec → `openapi-typescript` to TS
- Why: hand-keeping two type systems in sync rots fast.

**10. Config via env vars, not files.**

- `figment` or `envy` crate to load config
- All secrets (RPC API key, ClickHouse password) from env
- Never commit `.env` — only `.env.example`
- Why: Oracle VM deploy + Vercel frontend means you'll have config in two places. Env vars are universal.

**11. Backpressure boundary between fetch and write.**

- Bounded channel (`tokio::sync::mpsc::channel(100)`) between RPC fetcher and DB writer
- If writer is slow, fetcher blocks on send → naturally slows ingestion → respects rate limit
- Why: unbounded channels = OOM under DB stalls. Bounded = self-regulating.

**12. Health endpoint for the API.**

- `GET /health` → returns `{ last_slot, lag_seconds, db_ok }`
- Why: when frontend looks weird, first question is "is the ingester alive and current?"

**13. Don't precompute graph metrics yet.**

Tempting to materialize PageRank, centrality, communities upfront. Resist.

- v0: only raw edges + simple aggregates
- Add precomputed metrics when you have a query that's actually slow
- Why: you'll over-engineer based on imagined queries. Build heuristics after seeing real usage patterns.

**14. Frontend rendering boundary.**

You said graph viz. Browsers fall over at ~10k nodes with naive SVG/D3.

- Use Sigma.js, Cosmograph, or react-force-graph (WebGL-based) for 10k-100k nodes
- Always paginate / cap server-side — never let API return >50k edges in one response
- Why: a single bad query (`top wallets ever`) can crash a user's tab.

**15. Sentry / error reporting.**

- Free tier covers solo dev easily
- Catches frontend errors, backend panics, deploy issues
- Why: portfolio piece will be visited by people who don't tell you when it breaks. Sentry tells you instead.

**Things to explicitly NOT worry about for v0:**

- Authentication / multi-tenancy
- Horizontal scaling
- Distributed tracing (single-node, overkill)
- Real-time WebSocket push to frontend (poll is fine)
- Caching layer (ClickHouse is fast enough for v0)
- Microservices / message queues
- Kubernetes (single VM, just systemd)

Each of those is a real tool, but adding them now is anti-portfolio: it signals you've over-engineered a small project rather than executed cleanly on a focused one.

**The mental frame:** every architectural choice should answer "what breaks first when this grows or restarts." If the answer is "data integrity, ingestion lag, or silent failure," fix it now. If the answer is "we'd need more capacity," defer it.