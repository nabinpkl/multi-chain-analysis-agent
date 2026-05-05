# Must Do's
- Every backend feature change run docker compose up -d --build at end.
- Use latest docs for frontend and backend libraries before coding.
- For every library you need to add always search is it maintained. If not maintained we don't use it.
- Every article, blog post, doc page, or release note cited must have its publication date verified before being treated as a current source. Web-search results often surface stale content (1+ year old) ranked for relevance, not recency. Before quoting a fact from a URL: open the page or its metadata, find the published-or-updated date, compare against today, and if the article is older than ~6 months explicitly say so when reporting it. If the date can't be found, say "no date verifiable" instead of pretending the source is current. Apply this to research delegated to subagents too; the prompt must require dated citations and the subagent's report must show dates next to claims, not just URLs.
- Commit messages describe what the change does. Do NOT reference internal narrative scaffolding like "Ship N", "Pass M", "Session K", "Step X"  those are personal-plan vocabulary, not durable explanations a future reader can interpret. Reference a tracked issue (`#NNN`) when one exists; otherwise just describe the change and why. The commit subject is an action phrase ("add X", "fix Y", "refactor Z"), the body explains what the change does and the reasoning behind it. A reader six months from now should understand the commit from its message alone, without needing to know what arc or ship it belonged to.

# Don'ts
- No God component. Extract component if make sense.
- No dead code. Removed = delete entirely (files, imports, types, all refs).
- No backward compat layers. Iteration-based dev. Change code direct.
- No hand-typed wire types. Single source of truth (proto/`*.proto` files), generated to Rust + Python + TS via approved generators. Hand-typed allowed only for UI-internal models that never cross a service boundary.
- No relative imports in Python application code. Use absolute imports throughout (`from agent_service.policy.binding_store import X`, not `from ..binding_store import X` or `from .binding_store import X`). Application code lives in a fixed package; relative imports' main strength (surviving package-level renames during install) does not apply, while their cost (re-counting dots on every file move, copy-paste between files at different depths breaking silently, mid-file `from ..something` giving no clue what `something` is) IS real. Generated proto packages (`multichain.wire.*`) and third-party libraries naturally use absolute and stay that way.

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

Single source of truth for every type that crosses a service boundary lives in `proto/multichain/wire/{shared,agent}/v1/*.proto` (Protocol Buffers). Four approved tools (all maintenance-bar pass):

| Tool | Role | Last release at adoption |
|---|---|---|
| `buf` CLI (Buf Inc) | proto lint + breaking-change detection + codegen orchestration | 2026-04-29 |
| `buffa` (Anthropic) + `protoc-gen-buffa` | Rust types: pure Rust, JSON serialization, zero-copy views, editions support | 2026-04-27 |
| `protobuf` (Google official) + `protoc --python_out` | Python types | 2026-03-20 |
| `@bufbuild/protobuf` + `@bufbuild/protoc-gen-es` (Buf Inc) | TypeScript types: ESM-native, full type safety | 2026-04-23 |

Generated artifacts are checked in (consumers never need codegen to build). `just regen-wire-types` re-runs all three flows; CI fails if regenerated output differs from checked-in.

Anything authored as a Rust struct, Python pydantic model, or TS interface that crosses a service boundary is a bug. Add the message to `proto/`, run `just regen-wire-types`, import from `*_/wire/generated/` (or `frontend/src/lib/wire/`).

## Wire format per hop

Protobuf supports two wire encodings: binary (compact, ~3× smaller, ~5× faster) and canonical JSON (well-specified spec, browser-friendly). Pick by hop, not project-wide:

| Hop | Wire format | Content-Type | Why |
|---|---|---|---|
| Browser → Python `/agent/*` | proto canonical JSON | `application/json` | Browser fetch + camelCase TS-friendly |
| Python → Browser `/agent/stream/{id}` (SSE) | proto canonical JSON in SSE `data:` | `text/event-stream` | EventSource is text-only |
| Python → Rust `/primitive/*` | **binary protobuf** | `application/x-protobuf` | Service-to-service; both speak proto natively |
| Python → Rust `/turn/{begin,end}` | **binary protobuf** | `application/x-protobuf` | Same |
| Browser → Rust `/health`, `/graph/*` | proto canonical JSON | `application/json` | Browser + curl-debuggable |

Rationale:
- Browser hops MUST be JSON (EventSource is text-only, fetch + JS prefer JSON).
- Service-to-service hops use binary because both sides natively speak it. JSON between proto-speaking services is using the "browser exception" where the constraint doesn't apply. Per "Idiomatic-first": binary is the idiomatic protobuf wire for service-to-service.
- Rust HTTP routes MAY accept JSON as a fallback for `curl` debugging, sniffed via `Content-Type`. Production traffic from Python is always binary.

Canonical proto JSON encoding rules (the spec, applied automatically by all three runtimes):
- Field names: `snake_case` in `.proto` → `camelCase` on wire (default).
- Oneofs: `{"<active_case_name>": {<sub-message>}}`.
- Enums: full proto name as string (e.g. `"CLAIM_KIND_PROFILE"`).
- 64-bit ints: encoded as JSON strings (JS Number is 53-bit). Use `int32`/`uint32` in `.proto` for fields known to fit.
- Empty messages: `{}` (presence is the signal in oneofs).
- `optional` fields: omitted when not set.
- Bytes: base64. Timestamps: RFC 3339.

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