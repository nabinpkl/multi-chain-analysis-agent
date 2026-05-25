# Must Do's
- Every backend feature change run docker compose up -d --build at end.
- Use latest docs for frontend and backend libraries before coding.
- For every library you need to add always search is it maintained. If not maintained we don't use it.
- Every article, blog post, doc page, or release note cited must have its publication date verified before being treated as a current source. Web-search results often surface stale content (1+ year old) ranked for relevance, not recency. Before quoting a fact from a URL: open the page or its metadata, find the published-or-updated date, compare against today, and if the article is older than ~6 months explicitly say so when reporting it. If the date can't be found, say "no date verifiable" instead of pretending the source is current. Apply this to research delegated to subagents too; the prompt must require dated citations and the subagent's report must show dates next to claims, not just URLs.
- Commit messages describe what the change does. Do NOT reference internal narrative scaffolding like "Ship N", "Pass M", "Session K", "Step X"  those are personal-plan vocabulary, not durable explanations a future reader can interpret. Reference a tracked issue (`#NNN`) when one exists; otherwise just describe the change and why. The commit subject is an action phrase ("add X", "fix Y", "refactor Z"), the body explains what the change does and the reasoning behind it. A reader six months from now should understand the commit from its message alone, without needing to know what arc or ship it belonged to.
- After a substantive answer or change summary, append a short numbered list of plausible next directions. The list MUST surface alternatives, not collapse to a single path. Single-entry lists feel like tunnel vision and are forbidden; if you can only think of one direction, the rule is asking you to think harder about what genuine alternatives exist. The right shape is a mix of (a) the highest-priority next step within the current feature arc and (b) at least one pivot to a different direction. Calibrate count by feel: 2 when there's a clear priority path plus one real pivot; 3 when multiple priority paths or multiple pivots are both worth surfacing; 4+ essentially never. Each entry must be one line, concrete (specific file path, function name, or surface named), and substantive. Anti-patterns to avoid (these were the previous failure modes): padding to a fixed count regardless of situation; filler entries that don't earn their place; micro-iterations within the current feature that don't represent meaningful improvement (e.g. "rename a const", "split a function", "add a comment"); vague phrasing without a named surface. Default-banned filler categories: generic "open a tracked issue", "make the next commit", any commit / branch / housekeeping framed as a next-step entry. Docs are NOT default-banned but must earn the slot. A doc entry earns its place when it is (a) ADR-style capturing an architectural decision or pivot we just made, (b) documenting a genuinely new thing whose absence would lose information, or (c) landing a multi-turn resolution that has converged and needs a durable home. A doc entry does NOT earn its place when it's "stop and write a doc" / "update the architecture note" framed as a forward-progress option to fill a slot. Treat the list as suggestions only: the user may ignore it entirely and reply with an unrelated question, directive, or pivot. Do not phrase the entries as locked-in choices and do not assume the user is picking from the list. Format as `1.` / `2.` / `3.` so the user can reference by number.


# Don'ts
- No God component or god file or god functions. Extract/refactor when it feels like multiple isolated things are happening here at once but this shouldn't be done earlier than required.  When the agent encounters one of these while doing other work, the agent must call it out (don't silently work around it) and propose the extraction in the same change OR file a tracked issue if the extraction is too large for the current PR. Defending the existing shape with "matches what we have" or "minimizes churn" is the anti-principle the "Existing code is not authoritative" section already rejects; this bullet makes the size/shape signals explicit so the agent doesn't miss them.

- No dead code. Removed = delete entirely (files, imports, types, all refs).
- No backward compatibility layer. Iteration based dev where we clean up the mess as we go. **No parallel paths to the same outcome, even temporarily.** The "no backward compat layers" rule is a special case of this: at any moment in `git log`, there must not be two ways to achieve the same observable behavior unless there's an explicit justification recorded in the commit (e.g. a feature flag protecting a measurable risk). Splitting a refactor across multiple commits "for reviewability" is NOT a justification  it leaves a window where new types exist alongside the old code path they're meant to replace, and that window is exactly the dead-code shape the rule above forbids. When refactoring, ship the new path AND the cutover in one commit. If the change is too big to review as one commit, the unit of work is too big and should be split by feature, not by mechanical layer (foundation-then-cutover is the wrong split).
- No hand-typed wire types. Single source of truth (proto/`*.proto` files), generated to Rust + Python + TS via approved generators. Hand-typed allowed only for UI-internal models that never cross a service boundary.
- No relative imports in Python application code. Use absolute imports throughout (`from agent_service.policy.binding_store import X`, not `from ..binding_store import X` or `from .binding_store import X`). Application code lives in a fixed package; relative imports' main strength (surviving package-level renames during install) does not apply, while their cost (re-counting dots on every file move, copy-paste between files at different depths breaking silently, mid-file `from ..something` giving no clue what `something` is) IS real. Generated proto packages (`multichain.wire.*`) and third-party libraries naturally use absolute and stay that way.
- No Hardcoding of URLS, Ratelimits, configs etc if it feels like it should have been a entry inside .env config then proabably it should be.

# AGENTS.md is also iteration in progress

The rules below are this project's current working agreements, not commandments handed down from a finished design. They evolve. The same posture as "Existing code is not authoritative" applies here: when a rule conflicts with what the work demands, or when a section documents a state of the world that has moved on, propose the AGENTS.md edit in the same change. Do not silently work around an outdated rule. Do not defend a rule that's outlived its purpose by saying "AGENTS.md says so."

If something here seems to forbid the right move, either the rule needs revision or the move needs a justified exception recorded in the commit. Not "the rule says no, so we don't do it."

# Verify before asserting

The codebase is the source of truth. AGENTS.md is meta-guidance over it. Memory of "what file X contains" or "whether feature Y is implemented" is not authoritative; the file is.

Before stating what a function does, whether a primitive exists, what columns a table has, what a parser captures, or what has shipped: open the source. The cost of a Read tool call is trivial; the cost of building a plan on a wrong premise is high.

When verification isn't possible (file too large, no access, prior conversation summarized away the contents), say so explicitly. Calibrated uncertainty beats confident fiction. "I think X is the case but haven't verified" is fine; asserting "X is the case" without checking is not.

This applies to AGENTS.md itself. Sections below describe state of the world that may have moved on; treat them as historical context until the relevant code has been re-read in the current session.

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

# Planning vs coding scope

The "Existing code is not authoritative" rule applies more loosely during the conversation / planning phase than during the coding phase. Where the existing rule says "in the same change you're making, don't preserve mess to be polite," this one says "in the chat that scopes the change, don't defend mess to be polite either."

**During planning,** the agent should audit the adjacent code shape and surface patterns that don't fit, even when they're outside the strict ask. The cost of surfacing a "while we're in here, the existing X looks like a workaround, here's a cleaner shape" idea is one paragraph; the cost of NOT surfacing it is that the workaround gets entrenched as the new feature builds on top of it. Conservatism here ("the existing shape works, let's not expand scope") is the failure mode this rule corrects. If the agent finds itself recommending the option that touches less existing code purely because it touches less existing code, that is the signal to surface the alternative instead and let the user decide.

**During coding,** stay scoped to what was agreed in chat. Ride-along improvements that are small (a few lines, one file, no new abstractions) land in the same change. Improvements that are large (multi-file refactor, new abstraction, requires re-deciding a design choice) get filed as a tracked GitHub issue with enough context for a future session to act on, not silently deferred to "follow-up tickets that never get filed."

The split: chat is for surfacing options including ones that touch existing surfaces; coding is for executing the agreed option without scope creep.

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

See [SPEC.md  Wire contracts](SPEC.md#wire-contracts). The hop-by-hop encoding table and canonical-JSON rules live there; AGENTS.md keeps only the rule: every cross-service type lives in `proto/`, encoding is picked per hop (binary for service-to-service, canonical JSON for browser hops), and Rust may accept JSON only as a `curl`-debug fallback sniffed via `Content-Type`.

# Writing rules (docs/engineering-blogs/ only)

Apply when drafting/editing post content in `docs/engineering-blogs/`.

- No em-dashes. Read AI-written. Use periods, commas, colons, parens.
- No "X is not Y, it's Z" cadence unless earned.
- Keep numbers. Heavy lifting.
- First person, plain words, short paragraphs.
- Audience = peer engineers + technical hiring managers, not recruiters. Technical terms (O-notation, mmap, asymptotic) stay when advance story. Flex-for-flex's-sake (naming libs to sound senior) cut.
- Post = log, not content marketing. Skip hook-bait openers. Reader from resume link, not scroll.

# MultiChain Analysis Agent
Name: LLM analyst over a real-time Solana wallet graph

This is an agent-design exercise. The blockchain is chosen as the substrate because it produces real public high-volume data that forces clean ingest, idempotent writes, rate-limit discipline, and grounded narrative. The agent is the load-bearing part; the chain is the pressure environment that keeps the design honest.

## What It Is

Listen txs from multiple chain, normalize, link each tx to data, build graph, serve graph.

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



### Ingestion invariants

The 14 ingestion invariants (idempotency, durable checkpoint, rate-limit wrapper, batched writes, graceful shutdown, error categorization, structured logging, env-driven config, backpressure boundary, health endpoint, no-premature-metrics, frontend rendering caps, SSE backpressure) live in [SPEC.md  Ingestion invariants](SPEC.md#ingestion-invariants). What is in scope and out of scope today lives in [PRD.md](PRD.md).

**Mental frame:** every architectural choice answers "what breaks first when grows or restarts." If the answer is "data integrity, ingestion lag, silent failure", fix now. If the answer is "need more capacity", defer.

# Known Limitations

## Token metadata: resolved on-chain, attacker-controlled in display

Edges (`backend/src/ingest/parser.rs::parse_edges`) capture every wallet-to-wallet fungible movement by diffing pre/post balances across SOL and every SPL / Token-2022 mint, regardless of which program initiated the transfer. The `mint` column is empty for SOL and the mint pubkey for every other token. Mint-issuance and burn residuals are emitted as `kind="mint"` and `kind="burn"` edges using the mint pubkey as the synthetic peer.

On-chain metadata IS resolved via `backend/src/metadata/fetch.rs::fetch_token_metadata`, served through the `get_token_info` primitive: Metaplex Token Metadata PDA first, Token-2022 inline metadata extension as fallback. Cached in `multichain.token_metadata` (TTL ~1h). The agent has a `get_token_info(mint)` tool exposed via MCP.

The actual remaining gap is at the display layer. The `name` / `symbol` / `uri` fields are whatever string the mint authority embedded at creation time. Anyone can mint a Token-2022 with `name="USD Coin"` and `symbol="USDC"` at a non-canonical pubkey; the agent reads "USDC" from RPC and may narrate the wallet as transacting in USDC even though the actual mint pubkey is an impostor's. The mint pubkey itself is forge-proof (every SPL transfer references the mint pubkey directly, never a symbol), so data-layer queries are unambiguous; only the human-facing narrative is at risk.

Current defense: `agent_service.canonical_mints` holds a small allow-list of canonical pubkeys (USDC, USDT, wSOL). `stamp_verification` adds `verified: bool` plus `canonical_name` / `canonical_symbol` to the `get_token_info` payload; the prompt's `token_verification` rule instructs the model to use canonical labels when verified and qualify the symbol as unverified otherwise. The on-chain strings still pass through to the model as forensic surface; the verified flag is a tag, not a filter.

Out of scope today:
- URI decoding or off-chain JSON fetching. We pass `uri` through as a string only.
- LSTs (JitoSOL, mSOL, bSOL) and non-stablecoin majors (JUP, BONK, PYTH, WIF) in the canonical registry. Add when an eval shows a concrete narrative-quality miss.
- Adversarial-eval coverage of impostor-mint scenarios (synthetic Token-2022 with attacker-chosen `name` / `symbol` injected via test fixtures). The `judge-token-symbols-qualified` probe in `evals/cases/wallet_profile_smoke.yaml` is the placeholder rubric; the full suite (injection fixtures, cross-runtime parity) is the natural follow-on.

Memo text (`backend/src/ingest/parser.rs::parse_memos` + `multichain.memos` table) remains the other attacker-controlled outer-text channel; per the empirical study most memos are machine-generated routing tags rather than substantive prose, but the same `<external_data>` instruction-rejection rule applies.