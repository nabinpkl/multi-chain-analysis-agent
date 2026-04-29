


The thesis: "I built an analyst agent on real, live, large-scale data. Public data so no enterprise gate, but I implemented the same patterns you'd need in a regulated enterprise: typed action layer, capability scoping, provenance-attached claims, defense-in-depth prompt injection handling, append-only action ledger, plan-budget-execute. The blockchain substrate is what let me demo the patterns at real volume without negotiating data access."

That last line is the actual differentiator. Most candidates building agent infrastructure right now demo on toy data or synthetic enterprise schemas. You can show it running on Solana mainnet at 405 tx/s. The patterns transfer; the demo doesn't.

## Pattern by pattern, where each one lands a job

| Pattern in our app | What it becomes in enterprise context | Portfolio weight |
|---|---|---|
| Typed action layer over graph + warehouse primitives | Same layer over Snowflake / Postgres / FHIR / etc. | High. This is the "I don't write text-to-SQL because I know it doesn't work" talking point. |
| Capability-scoped read-only DB role | Same. Principle of least privilege. | Medium. Table stakes once they probe. |
| Defense-in-depth on untrusted text (3 layers) | Same. Customer-typed fields, document content, vendor data. | Very high. This is the "I take prompt injection seriously" talking point. Most candidates haven't thought past layer 1. |
| Provenance-required claims | Compliance, audit, "explainable AI". Healthcare, finance, legal all need this. | Very high. Highest-leverage pattern across all regulated industries. |
| Append-only action ledger with replay | SOC2, HIPAA audit logs. Also enables regression testing. | High. Especially when paired with eval suite. |
| Plan-then-execute with budget gate | Cost control on expensive warehouses. Snowflake credit budgets are real money. | High. "I think about cost, not just correctness". |
| Streaming claim slices | UX pattern, generalizes everywhere. | Medium. Nice but not differentiating. |
| Output-policy final pass | Toxicity / PII / off-topic prevention. | High. Same shape as enterprise guardrails on customer-facing chatbots. |

## What I'd add specifically for portfolio strength

These don't strictly defend anything we need in our context, but they're real patterns worth implementing because they sharpen the story:

**Eval suite with golden Q&A and regression tracking.** Pick 30-50 questions about the live graph that have known-correct answers (ones we can verify by hand: "how many MEV searchers in the last hour", "communities containing wallet X"). Run them on every change. Track accuracy and cost over time. This is the single highest-leverage portfolio addition because it shows engineering discipline most agent demos lack.

**Constitutional / policy check as a final pass.** Frontier pattern. A cheap second model reads the agent's output and judges against a written policy ("every claim must have provenance", "no advice on user actions", "no off-topic responses"). Doesn't need to be on the hot path, can be async + post-flag. This is the cutting-edge talking point.

**Primitive catalog as retrieval, not hardcoded prompt.** Instead of jamming all tool descriptions into the system prompt, store them in a vector index. Agent retrieves the relevant ones for the question. Much more enterprise-shaped (their schemas are bigger than your context window). Has the side effect of letting you scale primitives without prompt explosion.

**Cost attribution per primitive and per session.** Track LLM tokens, ClickHouse query time, total wallclock. Surface as a dashboard. "I know which kinds of analyses cost what" is concrete and rare.

**Replayable session traces.** Given a session ID, replay every tool call and output. Useful for debugging, regression testing, and demo: "here's an analysis, here's the full trace, here's me re-running it with one prompt change to show how the patterns hold up". Action ledger gives this for free if structured right.

## What I'd skip because it'd be theater here

These would pad the resume without doing real work in our context:

- **Fake multi-tenancy or RBAC.** No second user, no second tenant. Pretending to design for them is obvious to interviewers.
- **Approval-workflow stubs.** Read-only system; nothing needs human approval. Adding a "type APPROVE to continue" gate would be performative.
- **PII redaction layer.** No PII on Solana. Could be added "for the future tag database" but only when tags actually arrive.
- **Encryption at rest stories.** ClickHouse data is public. Demonstrating you'd encrypt it is silly.

When asked "did you do RBAC", honest answer is "no, it's a single-user demo, but the capability layer is the place I'd plug it in, here's what that diff would look like". That's a stronger answer than a fake implementation.

## The portfolio narrative arc

Three blog posts / talks / cover-letter paragraphs fall out naturally:

1. **"I moved Louvain off the main thread"** (already shipped). Backend analytics task, snapshot pattern, runtime A/B toggle. Talks to systems engineering.
2. **"I built an analyst agent that doesn't hallucinate SQL"** (about to ship). Typed actions, provenance-required claims, prompt injection defense. Talks to applied AI engineering.
3. **"How I evaluate an analyst agent"**. Eval suite, regression tracking, cost attribution, replayable sessions. Talks to engineering discipline.

Three different talking points. Same project. Different audiences (systems people, AI people, ops/quality people). One repo demonstrates all three.

## Where this lands

If you're sold on the framing, the plan I'd write would include:

- Five to seven typed primitives over the existing GraphState + ClickHouse
- Three-layer untrusted-text defense
- Provenance-required Claim type as the agent's only output unit
- Streaming claim slices to a chat sidebar
- Append-only action ledger
- Plan-budget-execute pattern
- Eval suite with golden Q&A scaffold
- Constitutional check pass (async, post-flag)
- Cost attribution telemetry

The skip list (theater in our context) gets called out in the README so the portfolio framing is explicit, not implicit.

Want me to draft that plan? Or push on a sub-question first, like which primitives to commit to, or which model to target.

# Industry patterns








**The actual fork**

Three serious shapes for an LLM-as-analyzer:

1. **Free-form SQL sandbox.** Agent writes SQL against ClickHouse, executor runs it under read-only role + row cap + timeout + EXPLAIN check. Most flexible, most failure modes, hardest to make fast. Text-to-SQL is a graveyard.

2. **Primitive tools.** A small kit of bounded, graph-shaped reads (`wallet_profile`, `neighborhood`, `community_members`, `path_between`, `time_window_diff`, `top_by_metric`). Agent composes them. Less flexible but each call is safe by construction and cacheable.

3. **Plan-and-execute.** Agent emits a typed DAG of primitive calls; a separate executor runs the plan, returns results, agent narrates. Adds a layer over (2).

You said "don't hardcode queries". That can mean two different things:
- "Don't ship a finite list of analyses the user can run" (achievable with any of the three)
- "Let the agent author SQL freely" (only option 1)

I'd push for **(2) primitive tools** as the starting point for this specific app, because the value is graph-shaped, not SQL-shaped. Questions like "what does this wallet do" or "is this a rotation ring" decompose more naturally into `neighborhood + classify + time_diff` than into joins. The agent doesn't have to learn the schema; it composes labeled operations. Adding (3) on top later is mechanical if (2) is good.

If we did (1), the failure mode isn't bad SQL, it's the agent hallucinating columns under load and us paying for the round trip. Solvable but more babysitting.

**What primitives would look like for this domain**

Brainstorm, not committed:

- `wallet_profile(addr, window)` returns role, community, top counterparties, in/out/bidir, age in window
- `neighborhood(addr, depth, window)` k-hop subgraph, capped at N nodes
- `community_members(community_id, window)` with stats
- `path_between(addr_a, addr_b, max_hops, window)` shortest path + intermediaries
- `top_by_metric(metric, role_filter, window, n)` e.g. top-10 mev-searchers by tips_touched in last hour
- `time_window_diff(window_a, window_b, metric)` "what changed between 09:00 and 10:00"
- `tag_lookup(addrs)` known-label hits (Jito tippers, exchange wallets, mint authorities) if we maintain a list
- `clickhouse_aggregate(metric, group_by, time_range, where_filter)` the safety hatch for things primitives don't cover, with strict where-clause grammar (not SQL string)

Each is read-only, time-bounded, row-capped. No primitive that mutates anything. No primitive that takes arbitrary SQL.

**Guardrails I'd want honestly**

- Read-only DB role at the connection level. Belt and suspenders with primitive-only design.
- Per-call: row cap (e.g. 1000), time-window cap (e.g. 24h max for historical, 1h for live)
- Per-session: tool-call cap (50?), ClickHouse query-time budget (10s aggregate?), token budget
- Streaming the agent's reasoning + tool calls to the UI so the user can interrupt
- Telemetry on every call so a flaky agent shows up as a bill, not a mystery
- Cache by (primitive, args) within a session so a re-asked question doesn't re-burn

**What I'd want answered before writing a plan**

I don't want to design this in the dark, three real questions:

1. **Analyst persona.** Who's actually using this? "I think a wallet is a drainer, prove it" is a different product than "what's interesting in the last hour" is different from "profile this counterparty for me before I sign". Each picks different primitives.

2. **Latency target.** "Type, get streamed answer in 5-30s" is a chat product. "Click a button, get summary in <2s" needs pre-cached or limited-scope analysis. They're not the same architecture.

3. **Output surface.** Sidebar chat with clickable wallet refs that highlight on the graph? Standalone analysis page? JSON for downstream tools? Drives whether we're building a UI feature or an internal tool.

chat sidebar in the existing app, 5-30s latency tolerated, output is markdown with wallet refs that highlight on the graph when clicked. Persona is "me, exploring patterns I notice in the breathing graph". That's the smallest useful thing.


Good. The horror stories you mentioned are the right reference points, but they're three different failure modes pretending to be one:

- **McDonald's**: prompt injection through user-controlled text bypassing role separation. Agent did exactly what the (attacker-shaped) input told it to.
- **DB-delete agent**: principle of least privilege violated. Agent had write credentials it shouldn't have ever held.
- **Replit auto-exec**: blast radius leak. Agent's actions affected the user's environment instead of a sandbox.

For our app, the threat model is different from any of those, and naming it precisely is what unlocks the design.

## Threat model for crypto-graph analyst agents specifically

What CAN'T happen here:
- Data leak: chain data is public.
- Customer-billing bug: there are no other tenants.
- DB destruction: we use ClickHouse with a read-only role for the agent's connection.

What CAN happen:
1. **Compute exhaustion**: agent issues an unbounded query that pegs the box. The Oracle Free Tier VM has 4 cores. One bad agent loop is enough.
2. **Token burn**: agent gets stuck in a tool-call loop, costs real money silently.
3. **Confident wrong claims**: agent says "this wallet is a drainer" with no traceable evidence, user acts on it. This is the highest-impact failure mode and it's invisible.
4. **Data-borne prompt injection**: a wallet's memo field, an SPL token's name, a future tag we add — the AGENT reads these as text. If we ever wire up labels from external sources, those become the attack surface.
5. **Adversarial graph structure**: someone shapes their on-chain activity to fool the classifier and the agent that reads classifier output. Less "injection", more "the data is the attacker".

Framing the design around those five is more useful than copying SOC2 patterns from b2b SaaS.

## Frontier patterns worth knowing about

Roughly in order of how mature they are right now:

**Capability-scoped credentials.** Agent's DB connection is read-only at the role level, query timeout at the connection level, max-rows at the driver level. Not novel but always the foundation. Without this, everything else is theater.

**Typed action layer instead of free SQL.** Same as primitive-tools but the right abstraction: agent emits a *request* in a typed schema, the executor validates, then translates to bounded SQL. The agent never sees a connection, never holds a transaction. This is the dominant pattern at companies that ship analyst-agents in production today.

**Plan-then-execute with a budget gate.** Agent emits a plan as structured data. A separate cheap model (or static rules) checks the plan against a budget: estimated rows, estimated tokens, estimated latency. Plan executes only if under budget. Out-of-budget plans are returned to the agent with feedback. This catches "search the entire 90-day warehouse" before it runs.

**Dry-run with sample.** Agent runs against a 1% sample first, shows the user the shape of the answer, user approves before full execution. The pattern Manus and a few others made popular. Reduces token burn on bad reasoning paths and gives the user a chance to redirect early.

**Provenance-required claims.** Every assertion in the final output must reference the tool calls and rows that produced it. Enforced structurally: the agent can't write a claim without attaching IDs. Renders the "confidently wrong" failure mode legible. Auditable, and forces the model to actually do the work.

**Containment-first sandboxing.** Agent runs in an ephemeral container with no network egress except to whitelisted endpoints (ClickHouse read-only port, the LLM API). This is the lesson from the Replit incident. Even if we never give the agent code execution, building this makes future moves (DuckDB-in-the-loop, embedded Python for stats) safe to add later.

**Output sanitization at the rendering boundary.** Whatever the agent emits gets parsed as structured data (markdown + typed claim references), never injected as raw HTML or executed. Cross-site-scripting through agent output is a real failure mode that's bitten people. Mostly defended by "render markdown via a known-safe renderer", but easy to forget.

**Ledger of every action.** Every tool call, every prompt, every response, hashed and append-only. Replay any session. Find regressions. Prove a claim was generated by call X with input Y. This is the highest-leverage thing nobody builds early enough.

## What's actually cutting edge in 2026

Two patterns I'd genuinely call frontier, both relevant to us:

**Constitutional / multi-judge checking.** A second model with a much shorter prompt judges every claim against a written constitution: "claim must reference specific edges", "no statement of intent without quantitative basis", "no advice on what the user should do". This is much more powerful than rules because it generalizes, but it doubles cost. The clever play is running it asynchronously and post-flagging rather than blocking, when the underlying call is read-only and recoverable.

**LLM-as-validator for the typed action layer.** Before a query executes, a cheap model reads it and asks "does this make sense for the question being asked, given the schema". Catches the failure mode where the agent issues a syntactically valid query that doesn't answer what the user actually asked. Still rare in production but it's where the cost-quality frontier is moving.

The pattern that hasn't been done well anywhere yet, and which I think is the actual greenfield for us: **graph-shaped primitives + provenance**. Most analyst agents today are shaped for tabular data because that's where the warehouses are. We have a graph. A primitive like `find_rotation_rings(min_size, time_window)` returns not just rows but a typed subgraph with provenance per edge. The agent narrates over typed subgraph evidence. This is genuinely novel territory because most public agent benchmarks don't include graph-shaped data. It's also what makes this app interesting beyond "another text-to-SQL UI".

## What I'd actually advocate

If you want to push the frontier rather than rebuild SQLpilot:

1. **Typed action layer over graph + warehouse primitives.** Foundation. Read-only, capped, budgeted. Five to ten primitives is enough for v0.
2. **Provenance-required output.** Every claim references edges/wallets/numbers. Structurally enforced. Renderer turns refs into hover-able UI links into the live graph.
3. **Plan-then-execute with budget gate.** Cheap and catches the worst burns.
4. **Append-only action ledger.** Day-one cost is small, day-365 value is enormous.
5. **Skip dry-run-with-sample and constitutional checking for v0.** They're frontier but they double the moving parts. Add them when we observe failure modes that justify them.

The novelty we get for "free" by being a graph-analytics app is the typed-subgraph-with-provenance output shape. That's not a guardrail, that's the product. The guardrails are conventional, the format is what makes the agent's claims auditable.

## Open questions before I'd commit to a plan

Three I think we still need answered:

1. **Are we ever going to run code the agent writes?** If yes (DuckDB-in-the-loop, on-the-fly Python for stats), we need the containment-first sandbox now. If no (typed actions only), we don't, and we save weeks. My instinct is "no, ever". You may want yes.

2. **Do we maintain a wallet-tag database?** If yes, that's the prompt-injection surface, and we need an "untrusted text" handling pattern from day one. If no, the threat model shrinks meaningfully.

3. **Streaming partial results to the UI vs returning a single finished analysis?** Streaming is much better UX, much harder to ledger and replay, much harder to gate on budget mid-stream. A real fork.

If I had to guess your defaults: no code execution, eventually a tag database (third-party labels are too useful), streaming responses with structured claims. That's the spec I'd write a plan against. Push back if it's wrong.