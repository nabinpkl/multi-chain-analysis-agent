
# When streaming how do work on 1 event vs collapse N events into 1 execution works?

## The misconception

Delaying a single event by 10ms doesn't help. If 1 expire = 1 rebuild, doing it now or 10ms later is the same total work. The delay would just be latency for nothing.

Debouncing is **not** "delay every event." It's "**collapse N events into 1 execution.**"

## What's actually getting batched

The critical observation: the rebuild doesn't process individual expired edges. It walks the *current* state of `graph.edges()` from scratch and rebuilds UF wholesale. So the cost of one rebuild is the same whether it was triggered by 1 expire or 50 expires.

Without debouncing:

```
expire #1 → rebuild (10ms work)
expire #2 → rebuild (10ms work)
expire #3 → rebuild (10ms work)
...
expire #50 → rebuild (10ms work)
```

50 events = 500ms of CPU for the same final answer.

With a 10ms debounce window:

```
expire #1 at t=0  → schedule rebuild for t=10ms
expire #2 at t=2  → already scheduled, do nothing
expire #3 at t=4  → already scheduled, do nothing
...
expire #50 at t=9 → already scheduled, do nothing
t=10ms            → rebuild runs once (10ms work)
```

50 events = 10ms of CPU, same final answer.

**The "batching" is of the WORK requests, not of the data.** The 10ms window is the time during which redundant rebuild requests collapse into one. The rebuild itself reads cumulative state from graphology, so all 50 expires are reflected.

This is exactly because the rebuild is idempotent and scope-fixed. Running it once is the same as running it 50 times. Coalescing 50→1 is pure win.

## What "no batching" would look like

Imagine instead of full rebuild, our handler did targeted work *per expire*:

```ts
// Hypothetical incremental version
onEdgeExpired(src, dst) {
  if (couldThisEdgeBeABridge(src, dst)) {
    runBfsOnComponent(src);  // expensive per call
  }
}
```

In this world, each expire has its own work to do. Debouncing wouldn't help directly — you'd need to **batch the data** (collect all expired edges in a list, then process the list together with shared state to amortize cost).

That's a different pattern: collect-then-process. Often used when the per-event work has shared setup or can be done more efficiently together (e.g., one BFS rooted at a representative covers many split candidates).

For our rebuild, we don't need that pattern because the work is "throw it all away and recompute." The data IS implicitly batched in graphology itself by the time rebuild runs.

## Three patterns, when to use each

| Pattern | Shape | Good for |
|---------|-------|----------|
| **Immediate** | event → action now | When per-event work is unique and cheap |
| **Debounce** | event → schedule one; collapse N→1 | When work is idempotent / reads-cumulative-state |
| **Batch** | event → push to buffer; flush buffer periodically | When per-event work shares setup / can amortize |

Our UF rebuild is "reads cumulative state from graphology" → debounce is the right pattern. We don't need a batch buffer because graphology *is* the buffer.

## The 10ms specifically

Why 10ms and not 0ms or 100ms?

- **0ms (no delay, just `Promise.resolve()`)**: still collapses N→1 within a microtask tick. Effectively "execute at end of current event-loop iteration." Works when all events arrive synchronously in one tick. SSE events don't always — they spread across event loop iterations.

- **10ms**: covers a few event-loop iterations. Captures bursts that span several SSE message ticks. Loose enough to catch typical window-slide burst, tight enough that staleness is invisible.

- **100ms**: catches even slow trickles into one rebuild. But staleness is now visible (1/10 of a second is noticeable). Probably overkill.

10ms is a heuristic. 5-50ms range is all defensible.

## Variant: do we want to *reset* the timer on each event?

The pattern I sketched does **not** reset. First event schedules for 10ms later, subsequent events within those 10ms don't extend the window:

```
expire at t=0   → schedule for t=10
expire at t=5   → already scheduled, no change
expire at t=8   → already scheduled, no change
t=10            → rebuild runs
expire at t=11  → schedule for t=21
```

**Resetting variant** (true trailing-edge debounce): every event clears the previous timer and sets a new one for 10ms later:

```
expire at t=0   → set timer for t=10
expire at t=5   → cancel, set new timer for t=15
expire at t=8   → cancel, set new timer for t=18
expire at t=12  → cancel, set new timer for t=22
... continuous events keep pushing the timer out
```

This **starves** under continuous events: if expires keep arriving every 9ms, the timer never fires. Used for things like search-as-you-type where you want to wait until typing stops.

For our case, starving would be bad: we want UF to converge during bursts, not wait until the burst ends. So **non-reset variant** (what I sketched) is right.

## Subtle thing: state vs scheduling lag

One more nuance. The 10ms delay means: **state can be up to 10ms stale even though all the data is already in graphology.**

That's fine for our use case (UF stale by 10ms is invisible) but worth being aware of. If we ever need millisecond-fresh UF, we'd lose this delay.

## Decision check

Given the above, here's the question worth answering: **is 10ms staleness on UF acceptable?**

- Visual layout ticks at 30Hz (33ms). UF stale by 10ms is well below one frame.
- Stats panel updates on graph mutation. 10ms stale means the count flickers slightly during bursts, then settles.

Both are clearly fine. So the 10ms debounce is the right pattern here, with batching happening implicitly through graphology's mutation state.

## TLDR

- Pure delay = no value. You're right to interrogate.
- Debounce = N→1 work coalescing during a window. Value comes from collapsing redundant idempotent work.
- Our rebuild reads cumulative state from graphology, so debounce is exact-fit; no separate batch buffer needed.
- Non-reset variant prevents starvation under continuous bursts. State always converges, bounded by 10ms staleness.
- 10ms is a pragmatic choice; anywhere in 5-50ms works.

Want me to ship the non-reset 10ms debounce now that the pattern's clear?