# How I use the Governor pattern in Rust

I was hitting the public Solana mainnet RPC. The published per-IP cap is 100 requests per 10 seconds globally, 40 per 10 seconds per method, and 40 concurrent connections. The block ingester wants to call `getBlock` roughly twice a second to keep pace with the chain. The agent's `/primitive/get_token_info` wants to call `getAccountInfo` whenever a wallet narration mentions an unknown mint. Both share one IP. If either one drifts above its budget, the other one gets 429s.

This is a textbook rate-limit problem. What I did not want was a sprinkling of `sleep_until(next_slot_at)` calls scattered across modules, with a comment in each one reminding the next reader to add a sleep here too. That kind of discipline rots. The first refactor that forgets it produces 429s in production, and you find out from the upstream provider's logs, not yours.

So I reached for the `governor` crate.

## What governor gives you

`governor` is a token-bucket rate limiter. You construct one with a `Quota` (one token every N milliseconds, optionally with a burst budget), and then any code path that wants to make a rate-limited call does:

```rust
limiter.until_ready().await;
// actually make the call
```

`until_ready` is an async function. It yields immediately if a token is available, and parks the task otherwise until one is. Token replenishment happens on a wall clock; you do not pump it manually.

Three things make this a good fit for a Rust async codebase:

1. The limiter holds its state in-process. No Redis dependency, no out-of-band coordination. Good for single-binary deploys; you would want something cluster-aware if you ran multiple ingester replicas.
2. `RateLimiter` is `Send + Sync` and cheap to clone behind `Arc`. You build one and hand it to every caller; the bucket is shared.
3. `until_ready().await` cooperates with Tokio. A waiting caller does not pin a thread; it yields to the runtime like any other I/O.

## The shape I landed on

The pattern I use: build one `RateLimiter` per logical lane, hold it in an `Arc` inside a client struct, and gate every method on that lane through `until_ready()`. Here is the actual code from `backend/src/rpc/client.rs`:

```rust
use governor::clock::DefaultClock;
use governor::state::{InMemoryState, NotKeyed};
use governor::{Quota, RateLimiter};

type Limiter = RateLimiter<NotKeyed, InMemoryState, DefaultClock>;

pub struct RpcClient {
    http: Client,
    url: String,
    ingester_limiter: Arc<Limiter>,
    primitive_limiter: Arc<Limiter>,
}

async fn call_with<T: DeserializeOwned>(
    &self,
    method: &str,
    params: Value,
    limiter: &Limiter,
) -> Result<T, RpcError> {
    limiter.until_ready().await;
    // actually make the HTTP call
}
```

Two lanes, not one. The ingester's `getBlock`/`getSlot` traffic and the primitive's `getAccountInfo` traffic each get an independent bucket. The HTTP client and the upstream URL are shared; only the limiter differs. If the agent gets chatty and burns its `primitive_limiter` budget, the ingester's `ingester_limiter` is untouched, and block fetch keeps cadence.

The lane split is the part I want to call out. If you have two workloads sharing an upstream, with different latency tolerances, a single shared limiter is the wrong choice. The agent can wait 500 ms for a token-info read; the ingester cannot wait 500 ms for a block read without falling behind the tip. Two limiters, sized differently, let each lane drift independently up to its own budget.

## How I size the lanes

The defaults in `.env.example`:

```
RPC_INGESTER_MIN_INTERVAL_MS=1000   # 1 req/s
RPC_PRIMITIVE_MIN_INTERVAL_MS=2000  # 1 req/2s
```

Combined that is 1.5 req/s, comfortably under the 10 req/s per-IP global cap with headroom for the occasional burst. I sized the ingester at 1 req/s because Solana mainnet produces a slot roughly every 400 ms; 2.5 slots/s is the ideal cadence but the public RPC budget will not support it sustainably. 1 req/s falls a little behind the tip but stays within budget, and the ingester catches up on quiet windows.

The `build_limiter` helper turns the duration into a governor `Quota`:

```rust
fn build_limiter(min_interval: Duration) -> Limiter {
    let interval = if min_interval.is_zero() {
        Duration::from_millis(1)
    } else {
        min_interval
    };
    let quota = Quota::with_period(interval)
        .expect("min_interval must be > 0")
        .allow_burst(NonZeroU32::new(1).unwrap());
    RateLimiter::direct(quota)
}
```

`allow_burst(1)` means no bursting: the bucket holds at most one token. I keep burst at 1 because the upstream's per-method 10-second window does not benefit from bursting; if I let the bucket fill to N over a quiet period, the next N calls fire instantly and trip the global cap.

## What this pattern is good for

It is the right tool any time you have:

1. A bounded budget against an upstream, expressed in calls per unit of time.
2. Multiple in-process callers who would otherwise step on each other.
3. A runtime that cooperates with async (Tokio, async-std, smol).

It is the wrong tool when:

- You need cluster-wide enforcement. Two ingester replicas with an in-process limiter each will combine to 2x the rate. Use Redis or a shared rate-limit service for that.
- The upstream's budget is per-resource (per wallet, per session, per anything other than IP). Governor supports keyed limiters (`KeyedRateLimiter`) for this case; my use is the unkeyed variant.
- You need backpressure across a network boundary. Governor only knows about the in-process queue. If the upstream slows down independent of your call rate, governor will keep handing out tokens and your calls will pile up. Pair it with a bounded `mpsc` channel for the fetcher-to-writer hop and let the channel apply real backpressure.

## What I would change if I started over

Honestly, not much. The two-lane split is the only non-obvious choice and it falls out directly from "ingester and agent should not be able to starve each other". The defaults in env are tunable per-deploy, which matters because the bucket sizing depends on the upstream you are pointing at: a paid Helius endpoint has a different budget than the public mainnet RPC.

The one thing I would do earlier next time is wire telemetry into the wait time. `until_ready().await` blocks; if it blocks for non-trivial duration that is information. A counter that increments when the wait time exceeds, say, 200 ms, would surface lane-saturation events in Langfuse. I have not added that yet; it is on the list.

If you take one thing from this post: do not put rate limits in code review. Put them in a wrapper. The wrapper's signature is what the rest of the codebase sees. The discipline becomes "use this client", not "remember to sleep here".

The code lives at `backend/src/rpc/client.rs` if you want to read the full version.
