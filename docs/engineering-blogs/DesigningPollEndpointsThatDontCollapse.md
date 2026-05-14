# Designing poll endpoints that don't collapse under traffic

I was building the `/graph/overview` endpoint for this project. Frontend polls it every 10 seconds to refresh a live Solana flow graph. The endpoint runs three aggregate queries over a ClickHouse edges table, takes about 300ms cold. Simple enough.

The first instinct is to just serve the query on every request. It works for 1 user. At 100 concurrent browsers, you're doing 600 database queries per minute. At 1000 browsers, 6000. The endpoint is fine. ClickHouse is fine. Until it isn't.

## The cache is the obvious fix. The TTL value is the interesting choice.

I picked 10 seconds. Not because it felt right, because it falls out of two constraints.

**Constraint one: it must match the frontend poll interval.** Frontend polls every 10s. If the TTL is 60s, five of every six polls return a response the user has already seen. If the TTL is 1s, the cache is useless for 90% of the window. TTL equal to poll interval means every poll fetches genuinely new data, and nothing is wasted.

**Constraint two: it must be below the "feels stale" threshold.** Rough number from dashboard UX: about 15 seconds is where users start perceiving a live view as frozen. Ten is comfortably under.

With TTL=10s, the server does 6 queries per minute no matter how many browsers are open. Database load stops scaling with audience size. The number that moved: from `queries = clients * 6/min` to `queries = 6/min, flat`.

## Single-flight is the part people forget

A TTL cache alone is not enough. At t=11s the entry expires. If 50 clients arrive at t=11.01s, without protection, all 50 recompute in parallel. Fifty concurrent ClickHouse queries, the load spike the cache was meant to prevent.

The fix is to put the compute inside a mutex. First client acquires the lock, computes for 300ms, stores the result, releases. Clients two through fifty block on the lock, wake up after 300ms, see the fresh cache, return. One compute, 49 free riders.

The naive way to write a TTL cache gets you half the protection. The single-flight version gets the rest.

## When not to bother

If the endpoint is cheap (under 10ms), skip the cache. The complexity costs more than the queries.

If the result is per-user (filtered by auth), a shared cache doesn't apply. You'd need per-user caching, which has a different shape.

If the data truly must be up-to-the-second (trading, alerts), caching at the endpoint is the wrong tool. Push a stream instead. For this project, the overview graph is cached. A separate SSE endpoint, planned for the next iteration, handles "what just happened".

## The takeaway

Cache TTL is not a knob you guess. It is the alignment point between three things: how fast your consumer polls, how stale your users tolerate, and how much database protection you need. Pick the smallest number that still collapses concurrent load. For this project, that was 10. For yours, it might be 2 or 120. But it should be the answer to the question, not a round number.
