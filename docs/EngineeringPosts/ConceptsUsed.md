  1. "Correctness under constraint." The 5 req/sec cap, idempotent ingestion, ReplacingMergeTree, checkpoint-after-commit, bounded channels for backpressure. Thesis:  real systems live under real limits; here's how I designed for them. This is a systems-engineer story. Reviewers who care about distributed-systems thinking will eat this up.    structured logging + tracing from the start

  2. "$0 infra, zero attack surface." Single Oracle VM, Cloudflared tunnel, no open ports, single Rust binary, systemd, Vercel for frontend. Thesis: modern stacks don't need Kubernetes; discipline beats complexity. This is an infra-taste story. Reads a senior.   

  3. "The graph as the product." Everything upstream exists to feed a viz that makes chain activity legible. Thesis: data is only as good as what you can see. This is a product-engineer story. Leans on design taste more.  

  - Read-only DB role, full stop. The AI path gets a connection string that physically cannot DROP, DELETE, INSERT, UPDATE, or ALTER. Not "the prompt says don't" — the Postgres/ClickHouse user doesn't have the grant. Prompt injection is irrelevant if the permission doesn't exist.                                                     
  - Query allowlist or template layer. LLM picks from a fixed set of parameterized queries rather than emitting raw SQL. Or: LLM emits SQL that's parsed to AST,         validated against a whitelist of table/column/operation combos, rejected otherwise. Either way, raw LLM output never reaches the DB.                                   
  - Resource caps at the query planner. SET statement_timeout, SET max_memory_usage, row limits injected server-side. A bad query gets killed, not served.
  - Result size caps. Your AGENTS.md already says 50k edges max per response. Same rule applies here. LLM can't exfiltrate the graph by asking nicely.                   
  - Cost estimation before exec. EXPLAIN the query, reject if it's going to scan the whole thing. Cheap, impressive, rarely demoed.                                      
  - Audit log. Every NL query → generated SQL → execution time → rows returned, stored. Reviewable trail.       
