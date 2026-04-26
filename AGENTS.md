# Must Do's
- Every backend feature change run docker compose up -d --build at end.
- Use latest docs for frontend and backend libraries before coding.
- For every library you need to add always search is it maintained. If not maintained we don't use it.

# Don'ts
- No God component. Extract component if make sense.
- No dead code. Removed = delete entirely (files, imports, types, all refs).
- No backward compat layers. Iteration-based dev. Change code direct.

# Writing rules (docs/LinkedInEngineeringPosts/ only)

Apply when drafting/editing post content in `docs/EngineeringPosts/`.

- No em-dashes. Read AI-written. Use periods, commas, colons, parens.
- No "X is not Y, it's Z" cadence unless earned.
- Keep numbers. Heavy lifting.
- First person, plain words, short paragraphs.
- Audience = peer engineers + technical hiring managers, not recruiters. Technical terms (O-notation, mmap, asymptotic) stay when advance story. Flex-for-flex's-sake (naming libs to sound senior) cut.
- Post = log, not content marketing. Skip hook-bait openers. Reader from resume link, not scroll.

# MultiChain Analysis Engine

## What It Is

Listen txs from multiple chain, normalize, link each tx to data, build graph, serve graph.

## Infrastructure

**Oracle Free Tier VM:** 24GB RAM, 4 Ampere cores. Runs Rust binary + cloudflared. That's it.
- Next.JS Vercel deploy
**Security:** Zero open inbound ports. Cloudflare absorbs DDoS. Connection hygiene in Rust service.

## `frontend/` stack

- **Framework:** Next.js 16+, App Router, TypeScript, `src/` directory, `@/*` alias
- **Package manager:** pnpm
- **Styling:** Tailwind CSS v4 (CSS-first via `@tailwindcss/postcss`)
- **UI components:** shadcn/ui — all components installed in `src/components/ui/`
- **State:** Zustand v5 (client), TanStack Query v5 (server)
- **Animation:** motion v12 (`motion/react`)
- Color: All colors oklch, no # based (convert if needed).


## Backend Rust Service Architecture

### Decision: Axum
Axum 0.8.x = 2026 default. Built on Tokio, via Tower middleware for connection hygiene. Same runtime for batch (tokio::io file streaming) + serving. Single binary, single process.

### Core Crate Stack
#### All latest
tokio
axum
tower
serde
serde_json
rustc-hash         # FxHashSet — faster than std HashMap



**1. Idempotency on ingestion — non-negotiable.**

Will restart, replay, double-fetch. Make every write idempotent.

- Use `signature` (tx hash) as primary uniqueness key
- ClickHouse: `ReplacingMergeTree(version)` engine on edges table. Duplicates collapse on merge.
- Re-fetching slot N must produce same DB state as fetching once
- Why: will run `getBlock(N)` twice on restart. Without idempotency = phantom edges in graph.

**2. Durable checkpoint of last-processed slot.**

- Single-row table `ingestion_state` storing `(component, last_slot, updated_at)`
- Update **after** batch commits, not before
- On startup, read to know where to resume
- Why: without this, restart = re-ingest from genesis or skip slots silent. Both bad.

**3. Rate-limited RPC client wrapper.**

Hard 5 req/sec cap. Don't trust self to remember everywhere.

- Wrap RPC client in token-bucket limiter (`governor` crate)
- Set ~4/sec (headroom for bursts)
- Every call goes through wrapper. No exceptions.
- Why: violate cap = 429s, then progressive backoff, then bans. Self-imposed limit prevents drift.

**4. Batched writes, not single-row inserts.**

ClickHouse hates 1-row inserts. Buffer in memory.

- Accumulate edges in `Vec<Edge>` per ingestion worker
- Flush on: 10k rows OR 5 sec elapsed (whichever first)
- Use `tokio::sync::mpsc` channel between fetcher + writer
- Why: 1-row inserts can be 100× slower, cause part-fragmentation in ClickHouse.

**5. Graceful shutdown.**

`Ctrl+C` must not lose current batch.

- `tokio::signal::ctrl_c()` triggers shutdown
- Drain in-flight RPC calls, flush write buffer, update checkpoint, exit
- Why: ungraceful shutdown = re-ingest partial slots next start, wasted RPC budget.

**6. Error categorization.**

Not all errors equal. Decide once, encode in types:

| Error | Action |
|---|---|
| `-32007` skipped slot | Increment, continue (not an error) |
| `-32004` block unavailable | Wait, retry same slot (you're at tip) |
| 429 rate limit | Exponential backoff |
| 5xx / network | Retry with backoff, up to N attempts |
| Parse failure | Log + alert + skip slot (don't crash) |
| DB write failure | Retry batch, then crash if persistent |

Implemented as `enum IngestError` with explicit handling in loop.

**7. Structured logging + tracing from start.**

- `tracing` + `tracing-subscriber` (Rust ecosystem standard)
- Log: slot fetched, batch flushed, errors w/ context, rate limit hits
- JSON output in prod, pretty in dev
- Why: when things break 3am need timestamps, slot numbers, request IDs. Adding logs after = painful.

**8. Frontend ↔ backend type contract.** (Later)

Rust backend + Next.js frontend. Don't hand-write TS types.

- Generate TS types from Rust structs via `ts-rs` crate (derive macro)
- Or `utoipa` → OpenAPI spec → `openapi-typescript` to TS
- Why: hand-keeping two type systems in sync rots fast.

**9. Config via env vars, not files.** (Later)

- `figment` or `envy` crate to load config
- All secrets (RPC API key, ClickHouse password) from env
- Never commit `.env`. Only `.env.example`.
- Why: Oracle VM deploy + Vercel frontend = config in two places. Env vars universal.

**10. Backpressure boundary between fetch + write.**

- Bounded channel (`tokio::sync::mpsc::channel(100)`) between RPC fetcher + DB writer
- If writer slow, fetcher blocks on send → slows ingestion → respects rate limit
- Why: unbounded channels = OOM under DB stalls. Bounded = self-regulating.

**11. Health endpoint for API.**

- `GET /health` → returns `{ last_slot, lag_seconds, db_ok }`
- Why: when frontend looks weird, first question = "ingester alive + current?"

**12. Don't precompute graph metrics yet.**

Tempting to materialize PageRank, centrality, communities upfront. Resist.

- v0: only raw edges + simple aggregates
- Add precomputed metrics when query actually slow
- Why: will over-engineer on imagined queries. Build heuristics after real usage.

**13. Frontend rendering boundary.**

Graph viz. Browsers fall over at ~10k nodes with naive SVG/D3.

- Use Sigma.js, Cosmograph, or react-force-graph (WebGL) for 10k-100k nodes
- Always paginate / cap server-side. Never let API return >50k edges per response.
- Why: one bad query (`top wallets ever`) crashes user tab.

**14. Sentry / error reporting.**

- Free tier covers solo dev
- Catches frontend errors, backend panics, deploy issues
- Why: portfolio piece visited by people who don't tell you when broken. Sentry tells you.

**Things to explicitly NOT worry about for v0:**

- Authentication / multi-tenancy
- Horizontal scaling
- Distributed tracing (single-node, overkill)
- Real-time WebSocket push to frontend (poll fine)
- Caching layer (ClickHouse fast enough v0)
- Microservices / message queues
- Kubernetes (single VM, just systemd)

Each = real tool, but adding now = anti-portfolio: signals over-engineered small project, not executed cleanly on focused one.

**Mental frame:** every architectural choice answer "what breaks first when grows or restarts." Answer = "data integrity, ingestion lag, silent failure" → fix now. Answer = "need more capacity" → defer.

# Known Limitations

## SOL transfer parser (v0)

Ingester only captures SOL transfers via `SystemProgram::transfer` and `transferWithSeed` (top-level + inner instructions). Does **not** capture SOL movements bypassing `SystemProgram`, e.g. program-owned PDAs mutating lamports direct on accounts they own. Good enough for daily-graph viz but understates true SOL flow with complex DeFi programs. Revisit when fraud heuristics need stricter accounting (likely via `meta.preBalances`/`postBalances` diff as complementary signal). See `Tickets.md` backlog.