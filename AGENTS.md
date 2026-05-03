# Must Do's
- Every backend feature change run docker compose up -d --build at end.
- Use latest docs for frontend and backend libraries before coding.
- For every library you need to add always search is it maintained. If not maintained we don't use it.

# Don'ts
- No God component. Extract component if make sense.
- No dead code. Removed = delete entirely (files, imports, types, all refs).
- No backward compat layers. Iteration-based dev. Change code direct.
- No hand-typed wire types. Single source of truth (proto/`*.proto` files), generated to Rust + Python + TS via approved generators. Hand-typed allowed only for UI-internal models that never cross a service boundary.

# Existing code is not authoritative

Existing code in this repo is iteration in progress, not a specification to defend. Most of it started as a quick-and-dirty path that hardened by accident. When you (the agent) see ad-hoc patterns, spaghetti, copy-paste, dead branches, hand-typed wire shapes, adapter layers bridging two-things-that-should-be-one-thing, or anything that doesn't match the target language's idiom: **flag it and propose the cleanup in the same change**. Don't defend the existing shape with "matches what we have" or "minimizes churn."

When the work is a refactor, migration, or anything touching a wide surface, that's the moment to leave the surface cleaner than you found it. "Minimize churn" is an anti-principle when the existing surface is itself the problem.

Specific anti-patterns the agent should flag (not silently preserve):
- `snake_case` JSON over the wire when the consumer is JS / TS.
- Hand-typed wire types when codegen is available.
- Adapter / case-conversion layers between services.
- "Backward compat" code paths in a project with no real users.
- Functions, files, or branches that don't run in production.
- Stale TODOs that have been TODO for more than one ship.
- Re-exports that exist only to dodge a refactor that should have happened.

Bias toward proposing the cleanup in the same change that needs the touch. Don't defer cleanup to "follow-up tickets" that never get filed. The "no dead code" rule above already implies this; this section makes the behavior explicit so the agent doesn't preserve mess to be polite.

When unsure whether to flag a pattern: err on the side of flagging. Cheap to dismiss, expensive to live with.

# Idiomatic-first (top priority for consistency with industry)

Each language uses its own idiom. Never impose one language's conventions on another. When in conflict between "easier for our codebase" and "what the language community does," pick the community idiom.

- **TypeScript:** `camelCase` fields, named exports, ESM imports, `interface` over `type` for object shapes that may extend.
- **Python:** `snake_case` fields, PEP 8, type hints everywhere, `dataclasses` / `pydantic` for data classes.
- **Rust:** `snake_case` fields, `Result<T, E>` for fallible ops, `Option<T>` for nullable, `#[derive]` macros for boilerplate.
- **JSON wire:** `camelCase` field names. The JS/TS-ecosystem default and what every modern API (Stripe, GitHub, Vercel, Anthropic) uses. Backends that emit `snake_case` JSON (Rust serde default) explicitly opt out of the idiom.

Cross-language type sharing respects each side's idiom: the wire format is `camelCase` JSON, each language's generated types use that language's natural field-name casing, and the codegen handles the translation. Don't write a manual case-conversion adapter at the boundary; that's drift waiting to happen. Pick a codegen toolchain that does it for you (protobuf canonical JSON encoding does this by default).

Why this is the first priority: idiomatic code is faster to read for newcomers (familiar patterns), copies cleanly from upstream documentation and community examples, and avoids the friction of "this is non-standard because..." explanations every time someone touches the code.

# Library maintenance bar

Before adding ANY third-party dependency (runtime OR build-time):

1. **Latest release within 1 month** of evaluation date. Stable mature tooling that hasn't released in 6+ months is a red flag, not a virtue.
2. **≥100 GitHub stars.** Floor for "someone else has shaken out the bugs."
3. **Real human maintenance, not bot churn.** Check the merged-PRs list. If the last 10 are all from `renovate[bot]` / `dependabot[bot]` / similar, the project is in maintainer-abandoned mode. Move on.
4. **Open-issue triage signal.** Either zero open issues (small tool, owner closes aggressively) or a healthy ratio of closed-by-humans to opened. A 100+ open-issues backlog with no human responses in months = abandoned.
5. **Build-time tools are NOT exempt.** They have FS + network access during codegen and are a real supply-chain surface. Same bar as runtime.
6. **Check the README for deprecation notices.** A "no longer maintained" banner overrides green CI badges.
7. If a library fails the bar but no maintained alternative exists, document the gap in `docs/dependency-exceptions.md` with the specific risk accepted and the trigger condition for revisiting (e.g., "drop if maintained fork emerges by 2026-09").

Concrete anti-patterns:
- LiteLLM (March 2026 supply-chain attack, April 2026 SQLi CVE-2026-42208).
- Renovate-bot-only activity hiding maintainer abandonment (e.g. `openapi-typescript` at 2026-05 audit: 245 open issues, last 10 merges all from renovate, real bug reports unanswered for weeks).
- Stefan Terdell's `json-schema-to-zod` deprecated 2026-03; npm metadata still showed activity from outstanding PRs being merged before going dark. Always read the README.

# Wire type ownership

Single source of truth for every type that crosses a service boundary lives in `schemas/*.json` (JSON Schema). Three approved generators:

| Target | Tool | Output |
|---|---|---|
| Rust | `typify` (oxidecomputer) | `backend/src/wire/generated.rs` |
| Python | `datamodel-code-generator` | `agent-service/src/agent_service/wire/generated.py` |
| TypeScript | `@n8n/json-schema-to-zod` + `zod` runtime | `frontend/src/lib/wire-zod.ts` |

All three meet the maintenance bar above. The generated files are checked in (so consumers never need to run codegen to build). `just regen-wire-types` re-runs all three; a pre-commit / CI check fails if regenerated output differs from checked-in.

Anything authored as a Rust struct, Python pydantic model, or TS interface that crosses a service boundary is a bug. Add the type to `schemas/`, run `just regen-wire-types`, import from the generated module.

# Writing rules (docs/LinkedInEngineeringPosts/ only)

Apply when drafting/editing post content in `docs/EngineeringPosts/`.

- No em-dashes. Read AI-written. Use periods, commas, colons, parens.
- No "X is not Y, it's Z" cadence unless earned.
- Keep numbers. Heavy lifting.
- First person, plain words, short paragraphs.
- Audience = peer engineers + technical hiring managers, not recruiters. Technical terms (O-notation, mmap, asymptotic) stay when advance story. Flex-for-flex's-sake (naming libs to sound senior) cut.
- Post = log, not content marketing. Skip hook-bait openers. Reader from resume link, not scroll.

# MultiChain Analysis Engine
Name: Real-time Graph Engine for Multi-chain State

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
- **UI components:** shadcn/ui  all components installed in `src/components/ui/`
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
rustc-hash         # FxHashSet  faster than std HashMap



**1. Idempotency on ingestion  non-negotiable.**

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