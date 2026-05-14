
Now you're on the actual industry pattern. Let me name it.

## What you just described has a name

**CQRS with multiple read models from one event stream.** Sometimes called **Kappa architecture** when the stream is the source of truth.

Core rule: the **event stream is canonical**. Every downstream view is a projection of it. Different views for different questions, but they all derive from the same events.

## The shape

```
                   ┌──► in-memory state machine  ──►  live graph (hot)
Parser ──events──┤
                   └──► ClickHouse               ──►  historical / agent queries (cold)
```

One event stream, two **independent read models**. Neither reads the other. Both are projections.

- **Hot model** answers: *"what's the graph right now?"* — precomputed, always in RAM, microsecond reads.
- **Cold model** answers: *"what did wallet X do last Tuesday?"*, *"how did this cluster evolve over 30 days?"*, *"tell the agent to find anomalous flow patterns"* — SQL, full flexibility, takes 100ms–seconds.

They don't compete. They serve different questions.

## Why this is the right frame

Three things fall out of it cleanly:

1. **ClickHouse stops pretending to be a live-query backend.** Its job becomes what it's actually good at: ad-hoc analytical queries over history. Top-view never touches it.

2. **The AI-agent surface you hinted at has a natural home.** When you want "the graph is a thing you can query," that's the cold model's job. It's SQL-shaped, it has history, it's already indexed for analytical work. Live view doesn't care, agent doesn't care about freshness at sub-second granularity.

3. **The two models fail independently.** Hot crashes → lose live state, ClickHouse still has history, re-warm from tip OR optionally from ClickHouse. Cold crashes → live view keeps running, just no historical queries until it's back. Classic isolation.

## The honest tradeoff you accept

**Dual-write consistency.** If the parser writes to memory but the ClickHouse write fails, history now has a hole that memory doesn't. In big systems, people fix this by putting Kafka in the middle and letting both sinks consume from it independently — that's the full Kappa pattern. At our scale (single VM, one service, no exactly-once requirement) this is overkill. The pragmatic move is:

- Best-effort write to ClickHouse, don't block the live path on it.
- If ClickHouse is down, buffer or drop to log, keep live view running.
- History has occasional gaps during outages, which for a portfolio piece is fine. The live graph is the product; history is a bonus surface.

## Lambda, and why we don't want it

You'll see **Lambda architecture** (Nathan Marz, ~2011) mentioned in this space: two parallel pipelines, speed layer for live + batch layer for historical, same logic implemented twice. It's what the industry used to do. It's now considered outdated because you maintain the computation twice and they drift. Kappa replaced it by running *one* pipeline with multiple projections. That's what we're doing.

Don't build Lambda. We're not building Lambda. We're building one parse, two sinks.

## What this means concretely for v0

- Keep ClickHouse. Its role is *different* — not the live query backend, just the historical projection.
- Parser becomes a fan-out point: one `Edge` out, two destinations.
- Live API reads from the state machine. Period.
- Historical API doesn't exist yet. ClickHouse accumulates. When you want the agent surface, it's already there.

Cost of keeping ClickHouse: one container, the existing ingest code, a few MB/day of disk. Benefit: you don't throw away the path to the second surface you already know you want.

That's the clean version of what you said. Textbook CQRS, Kappa-shaped, minus the broker because at single-VM scale the broker would be ceremony.

Why kafka?


You've got it. Let me sharpen two points that fell out of this.

## The real framing: "raw events first" is the discipline

What you just articulated — **save raw events before deriving anything** — is a principle, not a tool. Kafka is the industrial version. The principle could technically be implemented with:

- An append-only file (JSONL on disk).
- A Postgres append-only table.
- An object store with one object per batch.

Kafka is the *right* expression of the principle because it bundles four properties you need anyway the moment you have more than one consumer of the log: **durability, ordering, replay, and per-consumer offsets**. Rolling your own with a file gets you the first two but forces you to reinvent the other two. That's where "Kafka earns it" even at modest scale — not because one consumer can't survive without it, but because adding a *second* consumer later (the agent query surface, a fraud detector, a CSV export) is free with Kafka and a rewrite without it.

## The v0 win you didn't mention

Right now we truncate on restart because there's no cheap way to rebuild 24h of state. That's a hack to sidestep a design gap. With Kafka in front:

- Restart behavior becomes "replay from offset 0" or "replay from offset at T-24h" — identical semantics, cleaner model.
- Dev-loop "reset the state" becomes the same operation as production "recover from crash." One code path, both use cases.
- The ClickHouse projection becomes pure derived state — you can drop its data anytime, replay from Kafka, and be back.

The current truncate-on-start stops being a hack. It's just "consumer starts at the tail," which is a legitimate Kafka consumer config (`auto.offset.reset=latest`).

## For portfolio-readable signal

The portfolio audience is engineers and technical hiring managers (per AGENTS.md). "I built this with Kafka because I wanted multiple consumers and a durable event log, here's the kappa diagram, here's how the state machine rebuilds from the log" reads as *thoughtful*. "I polled ClickHouse every 10 seconds" reads as *first draft*.

The story matters. The choice of Kafka here isn't over-engineering — it's the cheapest tool that *correctly expresses the architecture you want to show you understand*. That's a legit reason for a portfolio project. It'd be a bad reason for production at a startup with two engineers, but that's not this context.

## One practical note

If we go this way on the Oracle VM, don't run Apache Kafka. Use **Redpanda** single binary, single node. Kafka wire-compatible, no ZK, no JVM, one process, starts in 2 seconds, reads/writes at Kafka protocol. For v0 that's the honest fit. If the project ever graduated to multi-node, same binary, just add more.

---

Ready to translate this into an actual v0 architecture sketch when you are. Or if you want to keep working through the model first, what's the next thing that feels unclear?