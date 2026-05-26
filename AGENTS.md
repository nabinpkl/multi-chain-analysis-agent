# Must Do's
- Every backend feature change run docker compose up -d --build at end.
- Use latest docs for frontend and backend libraries before coding.
- For every library you need to add always search is it maintained. If not maintained we don't use it.
- Verify publication date before citing any article, blog, doc, or release note. Web search ranks for relevance, not recency.
  - Find the published-or-updated date on the page; compare against today.
  - Older than ~6 months: say so when reporting.
  - No date findable: say "no date verifiable", don't pretend it's current.
  - Delegated research same: subagent prompts require dated citations; reports show dates next to claims, not just URLs.
- Commit messages describe what the change does, readable six months out without personal-plan vocabulary.
  - Subject: action phrase ("add X", "fix Y", "refactor Z").
  - Body: what changed and why.
  - Reference `#NNN` when one exists.
  - Never reference "Ship N", "Pass M", "Session K", "Step X".
- After a substantive answer or change summary, append a numbered list of next directions (`1.` / `2.` / `3.`).
  - Must surface alternatives, not collapse to one path. Single-entry lists forbidden; if only one comes to mind, think harder.
  - Shape: (a) highest-priority next step in current arc + (b) at least one pivot.
  - Count: 2 = priority + pivot; 3 = multiple priorities or pivots worth surfacing; 4+ essentially never.
  - Each entry: one line, concrete (named file/function/surface), substantive.
  - Anti-patterns: padding to a count; filler; micro-iterations ("rename a const", "split a function", "add a comment"); vague phrasing.
  - Default-banned filler: "open a tracked issue", "make the next commit", any commit/branch/housekeeping as a next-step entry.
  - Doc entries earn a slot only when (a) ADR-style capturing a decision just made, (b) documenting a genuinely new thing whose absence loses info, or (c) landing a converged multi-turn resolution. Not earned: "stop and write a doc" to fill a slot.
  - Suggestions only. User may ignore or pivot. Don't phrase as locked-in choices.


# Don'ts
- No god components, god files, or god functions.
  - Greenfield: extract when multiple isolated things start happening in one place. Don't preemptively over-decompose; do it when required, not earlier.
  - Encountered mid-work: call it out (don't silently work around it) and either propose the extraction in the same change, or file a tracked issue if it's too large for the current PR.
  - Don't defend an existing god shape with "matches what we have" or "minimizes churn". The "code is not authoritative" rule below already rejects that.
- No dead code. Removed = delete entirely (files, imports, types, all refs).
- No backward compatibility layers. **No parallel paths to the same outcome, even temporarily.** Iteration-based dev means we clean up the mess as we go.
  - At any moment in `git log`, there must not be two ways to achieve the same observable behavior unless an explicit justification is recorded in the commit (e.g. a feature flag protecting a measurable risk).
  - "For reviewability" is NOT a justification. Splitting a refactor across commits leaves a window where new types coexist with the old code path, which is exactly the dead-code shape forbidden above.
  - Ship the new path AND the cutover in one commit.
  - If that commit is too big to review, the unit of work is too big. Split by feature, not by mechanical layer. Foundation-then-cutover is the wrong split.
- No relative imports in Python application code. Use absolute imports throughout: `from agent_service.policy.binding_store import X`, not `from ..binding_store import X` or `from .binding_store import X`.
  - Why: application code lives in a fixed package, so relative imports' main strength (surviving package-level renames during install) does not apply here.
  - Real cost of relative imports: re-counting dots on every file move; copy-paste between files at different depths breaking silently; mid-file `from ..something` giving no clue what `something` actually is.
  - Generated proto packages (`multichain.wire.*`) and third-party libraries naturally use absolute and stay that way.
- No hardcoding of URLs, ratelimits, configs, etc. If it feels like it should be a `.env` entry, it probably should be.

# AGENTS.md is iteration in progress
- Rules below are this project's current working agreements, not commandments. They evolve.
- When a rule conflicts with the work, propose the AGENTS.md edit in the same change. Don't silently work around it. Don't defend it with "AGENTS.md says so."
- Exceptions get recorded in the commit, not waved through.

# The code is authoritative, not memory or convention

Two related rules: verify reality before asserting, and don't defend stale shapes once you see them.

## Verify before asserting
- Codebase is the source of truth. AGENTS.md is meta-guidance over it. Memory of "what file X contains" or "whether feature Y shipped" is not authoritative; the file is.
- Before stating what a function does, what columns a table has, what a parser captures, or what's shipped: open the source. Read is cheap; building a plan on a wrong premise is not.
- When verification isn't possible (file too large, no access, prior conversation summarized away), say so explicitly. "I think X but haven't verified" beats confident fiction.
- Applies to AGENTS.md itself. Sections describe state that may have moved on; treat them as historical until re-read this session.

## Existing code is not authoritative
- Code in this repo is iteration in progress, not a spec to defend. Most of it started quick-and-dirty and hardened by accident.
- When you see ad-hoc patterns, copy-paste, dead branches, hand-typed wire shapes, adapter layers bridging two-things-that-should-be-one-thing, or non-idiomatic code: **flag it and propose the cleanup in the same change**. Don't preserve mess to be polite.
- Refactors and migrations touching wide surface are the moment to leave it cleaner than you found it. "Minimize churn" is an anti-principle when the surface itself is the problem.
- When unsure whether to flag a pattern, flag it. Cheap to dismiss, expensive to live with.

Specific anti-patterns to flag (not silently preserve):
- `snake_case` JSON over the wire when the consumer is JS / TS.
- Hand-typed wire types when codegen is available.
- Adapter / case-conversion layers between services.
- "Backward compat" code paths in a project with no real users.
- Functions, files, or branches that don't run in production.
- Stale TODOs that have been TODO for more than one ship.
- Re-exports that exist only to dodge a refactor.

# Planning vs coding scope

The "code is not authoritative" rule applies in planning too, not just coding. Chat surfaces options including ones that touch existing surfaces; coding executes the agreed option without scope creep.

**During planning:**
- Audit the adjacent code shape; surface patterns that don't fit even when they're outside the strict ask.
- One paragraph to surface "while we're in here, X looks like a workaround" is cheap; not surfacing it lets the workaround get entrenched as the new feature builds on top.
- If you find yourself recommending the option that touches less existing code purely because it touches less existing code, surface the alternative instead and let the user decide.

**During coding:**
- Stay scoped to what was agreed in chat.
- Small ride-alongs (a few lines, one file, no new abstractions) land in the same change.
- Large ride-alongs (multi-file refactor, new abstraction, requires re-deciding a design choice) get filed as a tracked GitHub issue with enough context for a future session, not silently deferred to "follow-ups that never get filed."

# Idiomatic-first (top priority for consistency with industry)
- Each language uses its own idiom. Never impose one language's conventions on another. When in conflict between "easier for our codebase" and "what the language community does," pick the community idiom.
- Idiomatic code is faster to read for newcomers, copies cleanly from upstream docs and community examples, and avoids "this is non-standard because..." friction every time someone touches it.

Per-language:
- **TypeScript:** `camelCase` fields, named exports, ESM imports, `interface` over `type` for object shapes that may extend.
- **Python:** `snake_case` fields, PEP 8, type hints everywhere, `dataclasses` / `pydantic` for data classes.
- **Rust:** `snake_case` fields, `Result<T, E>` for fallible ops, `Option<T>` for nullable, `#[derive]` macros for boilerplate.
- **JSON wire:** `camelCase` field names. JS/TS-ecosystem default; what every modern API (Stripe, GitHub, Vercel, Anthropic) uses. Backends emitting `snake_case` JSON (Rust serde default) explicitly opt out of the idiom.

Cross-language type sharing: wire format is `camelCase` JSON, each language's generated types use that language's natural casing, codegen handles translation. Don't write a manual case-conversion adapter at the boundary; that's drift waiting to happen. Pick a codegen toolchain that does it (protobuf canonical JSON encoding does this by default).

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

Generated artifacts are checked in (consumers never need codegen to build). `just regen-wire-types` re-runs all three flows; CI fails if regenerated output differs from checked-in.

Anything authored as a Rust struct, Python pydantic model, or TS interface that crosses a service boundary is a bug. Add the message to `proto/`, run `just regen-wire-types`, import from `*_/wire/generated/` (or `frontend/src/lib/wire/`).

## Wire format per hop

See [SPEC.md  Wire contracts](SPEC.md#wire-contracts). The hop-by-hop encoding table and canonical-JSON rules live there; AGENTS.md keeps only the rule: every cross-service type lives in `proto/`, encoding is picked per hop (binary for service-to-service, canonical JSON for browser hops), and Rust may accept JSON only as a `curl`-debug fallback sniffed via `Content-Type`.

# MultiChain Analysis Agent
Name: LLM analyst over a real-time Solana wallet graph

This is an agent-design exercise. The blockchain is chosen as the substrate because it produces real public high-volume data that forces clean ingest, idempotent writes, rate-limit discipline, and grounded narrative. The agent is the load-bearing part; the chain is the pressure environment that keeps the design honest.

## What It Is

Listen txs from multiple chain, normalize, link each tx to data, build graph, serve graph.

## Per-service stack rules

Stack picks and per-service conventions live in each service's own `AGENTS.md`. When working in one of these subtrees, that file is what to read first:

- **`backend/`** (Rust data plane): [backend/AGENTS.md](backend/AGENTS.md). Axum + Tokio, ingestion invariants, the `INTERNAL_PORT=8004` trust boundary, MCP host allowlist.
- **`agent-service/`** (Python agent plane, the load-bearing part): [agent-service/AGENTS.md](agent-service/AGENTS.md). Python 3.14 + uv, the two-runtime parity contract, the output-gate discipline, eval-judge family-leakage guard, ClickHouse parameterization, codex subprocess hygiene.
- **`frontend/`** (Next.js renderer): [frontend/AGENTS.md](frontend/AGENTS.md). Next.js 16+ App Router, Tailwind v4, shadcn/ui, oklch colors, generated wire types from `src/lib/wire/`.

Cross-service stack table with versions and notes: [SPEC.md  System topology](SPEC.md#system-topology) and [README.md  Stack](README.md#stack).

### Ingestion invariants

The 14 ingestion invariants (idempotency, durable checkpoint, rate-limit wrapper, batched writes, graceful shutdown, error categorization, structured logging, env-driven config, backpressure boundary, health endpoint, no-premature-metrics, frontend rendering caps, SSE backpressure) live in [SPEC.md  Ingestion invariants](SPEC.md#ingestion-invariants). What is in scope and out of scope today lives in [PRD.md](PRD.md).

**Mental frame:** every architectural choice answers "what breaks first when grows or restarts." If the answer is "data integrity, ingestion lag, silent failure", fix now. If the answer is "need more capacity", defer.