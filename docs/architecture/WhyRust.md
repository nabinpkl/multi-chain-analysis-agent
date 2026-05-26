# Why Rust for the data plane

The data plane is the ingester, the in-memory graph, the typed primitive surface, and the HTTP + MCP listeners that serve them. All of it lives in one Rust binary at `backend/`. This doc is the architectural rationale for that choice. The agent plane is Python on purpose for separate reasons (ADR [12-python-agent-migration](../../architecture-decisions/12-python-agent-migration.md)); this doc is about the data side only.

## The decision

One Rust binary, one Tokio runtime, one process. The runtime serves three concurrent workloads from the same scheduler:

- The ingester loop (`backend/src/ingest/runner.rs`) pulls Solana blocks at the chain's slot cadence and normalizes them into typed edges.
- The graph engine holds the live wallet graph in `FxHashMap`-keyed adjacency under a single `RwLock` (`backend/src/state.rs`, ADR [03-graph-engine](../../architecture-decisions/03-graph-engine.md)).
- The Axum-on-Tokio HTTP surface serves `/health`, `/graph/snapshot`, `/graph/stream` (SSE), `/turn/*`, `/primitive/*`, and `/mcp`.

The same runtime carries both the producer (block ingest) and the consumer (graph snapshot reads, agent primitive calls). There is no separate worker pool, no message broker between the ingester and the graph, no language boundary inside the data plane.

## What this design buys

**Memory cost of the live graph is predictable and small.** The graph runs in `FxHashMap` (a `rustc-hash`-backed `HashMap` faster than std for our key shapes) over plain `String` and `u64` keys. There is no GC heap, no boxing per node, no per-edge allocation overhead from an object header. Empirically the live 60-second window fits in well under 1 GB; a Java or Go equivalent would need 2-3x for the same node count. The host budget is single-host (per [PRD.md](../../PRD.md)); a heap that doubles silently under load is a failure mode that does not happen here.

**Concurrency without a runtime-shaped tax.** Tokio is built around `Arc<RwLock<T>>` for shared state and bounded `mpsc` channels for handoff. The ingester writes edges through one writer task; readers (`/graph/snapshot`, graph-stream SSE clients, the analytics task that recomputes Louvain every 3s, see ADR [09-louvain-snapshot-on-backend](../../architecture-decisions/09-louvain-snapshot-on-backend.md)) take the read lock briefly and release. No N+1 problem, no GIL, no async-coloring fight, no thread-pool sizing knob to tune. Backpressure rides on the bounded channels (invariant #10 in [SPEC.md](../../SPEC.md#ingestion-invariants)); when the writer stalls, the fetcher blocks on send, ingestion slows, the rate-limit envelope is respected, no out-of-memory event.

**The rate-limit budget is a typed wrapper, not a discipline.** `backend/src/rpc/client.rs::RpcClient` carries two independent `governor` token buckets: the ingester lane gates `getBlock` and `getSlot`, the primitive lane gates `getAccountInfo` for `/primitive/get_token_info`. Every RPC call goes through the wrapper. There is no escape hatch and no "just this once" code path. In a less-typed runtime this would be a convention enforced by code review; here it is a function signature.

**Single-binary deploy.** `cargo build --release` produces one ELF; `docker compose up` ships the same binary. There is no language runtime to install, no GC tuning, no JVM heap to size. The Dockerfile is short and the image is small. This matters less in theory than it does in practice: when the host is one machine and the operator is one person, every extra moving part has a real cost.

**Error categorization in types.** `enum IngestError` (`backend/src/ingest/error.rs`) gives the loop one match arm per recoverable category: skipped-slot codes increment and continue; `RateLimited` is exponential backoff; `Transient` retries; `Fatal` crashes the process. The compiler refuses to let the loop drop a variant. The 14 ingestion invariants (in [SPEC.md  Ingestion invariants](../../SPEC.md#ingestion-invariants)) have several that are "encode this in the type system, not in a runbook"; Rust is the only mainstream choice where the type system is strong enough to make that cost-free.

**One language, one debugger, one observability surface.** `tracing` + `tracing-subscriber` produce JSON-line logs in prod (`LOG_FORMAT=json`) and pretty logs in dev. Profiling, perf counters, async-aware backtraces; the toolchain treats async and sync code uniformly. Splitting the data plane across two languages would force a logging schema unification and a tracing-context-propagation contract; one language sidesteps that.

## What this design costs

**The agent plane is a different language.** Python owns the agent loop, the output gate, and the eval substrate. The two services talk over protobuf-on-HTTP (binary on the service-to-service hop, canonical JSON on the browser hop, per [SPEC.md  Wire contracts](../../SPEC.md#wire-contracts)). The split is intentional (ADR [12-python-agent-migration](../../architecture-decisions/12-python-agent-migration.md)) but it means there are two build systems, two dependency files, two test runners. The wire-type codegen (`just regen-wire-types`) is what keeps the two sides honest.

**Compile time.** Rust full builds are slow. The CI loop is on the order of minutes, not seconds. Incremental builds during local dev are tolerable; the cold rebuild after a dep bump is not. The mitigation is `cargo check` over `cargo build` during iteration and pinning dep versions in `Cargo.lock` aggressively.

**Async ecosystem fragmentation taxes a few choices.** Some upstream Rust crates do not have first-class Tokio support; the workarounds are usually a `spawn_blocking` wrapper or a thin `tokio-`-prefixed shim. So far this has cost a few hours over the lifetime of the project, but it is a real friction surface that a Go or Node equivalent would not have.

**Library choices are smaller than for older ecosystems.** Some of the ingest-side tools are Rust-first (rkyv, serde, governor) and have no Python or Go equivalent of comparable quality; others are mature elsewhere first (a ClickHouse client with the surface of `clickhouse-rs` is not yet at parity with Python's `clickhouse-connect`). When that gap matters, the agent service handles the workload (the eval probe queries against `otel.otel_traces` go through Python's `clickhouse-connect`, not the Rust crate).

## The alternative we rejected

**Go for the same data plane.** Cheaper compile times, easier learning curve, simpler concurrency model on small teams. Rejected because (1) Go's GC pauses, while small, are non-zero, and the SSE delta stream needs predictable latency under thousands of concurrent subscribers; (2) Go's lack of sum types means error categorization becomes a sprinkling of `if errors.Is(err, ErrRateLimited)` instead of an exhaustive match the compiler enforces; (3) the project is solo-built and "agent friendly" is a top consideration. Rust's type system makes wrong refactors fail to compile, which is the highest-leverage form of CI we have.

**Node.js for the data plane.** Cheap to write, large ecosystem, easy to share types with the Next.js frontend. Rejected because the ingester is doing nontrivial numerical work (balance diffs across hundreds of SPL accounts per block), and Node's single-threaded event loop combined with V8's allocator behavior under sustained throughput would force us into worker-thread complexity inside the data plane. Once you add a worker pool to Node, the simplicity advantage is gone; once it is gone, the type-safety gap is what is left. (The frontend stays on Node because it is rendering, not ingesting.)

**Python for the data plane too.** Cheap because it would unify the language across both services. Rejected because Python's `getBlock` parsing throughput would be 5-10x slower than the Rust equivalent, and the GIL makes the concurrent-reader pattern around the in-memory graph fight against the runtime rather than ride on it. Python is the right choice for the agent plane (ecosystem of LLM tooling lives there; agent code is glue-shaped); it is the wrong choice for the data plane.

## The contract with future ships

The data plane stays in Rust. The shape of "what is in the Rust binary" can grow (new primitives, new graph algorithms, new wire routes), but it does not fragment across languages. If a workload appears that genuinely does not fit Rust (something with a Python-only library that has no Rust analogue), the right move is to push it to the agent plane behind a typed wire contract, not to introduce a third runtime in the data plane.
