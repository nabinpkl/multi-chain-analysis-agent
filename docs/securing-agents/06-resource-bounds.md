# 06: Resource bounds as a defense

The lesson: an LLM agent under prompt injection can fail in expensive ways. A prompted-into-a-loop agent calls tools forever; a tricked-into-verbose agent burns tokens; a misrouted agent runs the same primitive 50 times. Most of these will not show up in your eval suite because the eval suite measures correctness, not budget. You need explicit caps on turns, tool calls, tokens, and time, with at least one regression case that proves the cap fires when an attacker tries to blow past it.

This is one of the layers most often built in piecemeal across files and runtimes and never written down as a coherent policy. Our codebase is currently in exactly that state. This chapter says what we have and what is missing.

## The attacks

Three flavors, in increasing subtlety.

**Runaway tool loop.** The user-question or a tool result tricks the agent into calling tools repeatedly. Even with each call cheap, 100 calls in a turn drains free-tier quota and stretches latency past the SSE-timeout. Hardest to provoke against a frontier model; easier on cheaper ones.

**Token-burn injection.** The injection asks the agent for a verbose response, an itemized list, a recap of everything. The agent complies and emits a huge narrative. The output verification pipeline does not have a length cap (a long correct narrative passes), so the cost lands silently.

**Quota-exhaustion via legitimate-looking traffic.** No specific injection needed. The user just hammers expensive turns. This shades into rate-limiting territory, which is separate from prompt-injection defense, but the failure mode is the same shape (cost without value).

## What we have today

Scattered, not unified, not documented.

- [core/run.py:88](../../agent-service/src/agent_service/core/run.py:88): `_USAGE_LIMITS = UsageLimits(request_limit=10, tool_calls_limit=8)`. The pydantic-ai loop's per-turn ceiling. Caps the number of model requests inside one turn at 10 and tool calls at 8.
- Codex side: an internal cap inside the codex helper. Not surfaced in our code as a constant, not documented.
- [thread_state.py:92](../../agent-service/src/agent_service/thread_state.py:92): `MAX_THREAD_TOOL_CALL_TURNS = 20`. Not a per-turn cap; this prunes thread state so memory and disk usage stay bounded across a long conversation. The 21st turn's data evicts the 1st.
- Free-tier OpenRouter has its own request rate cap. Operates as a de facto cost ceiling but is not under our control.

The result is partial coverage and inconsistent shape across the two runtimes. A turn that hits the pydantic-ai cap fails one way; the same payload under codex fails another way or not at all.

## What is missing

Five gaps, ordered roughly by severity.

1. **No unified per-runtime policy.** The pydantic-ai cap and the codex cap should match. Today nobody has checked whether `tool_calls_limit=8` on one runtime corresponds to the codex helper's internal cap on the other. If they diverge, eval probes that pass on one runtime can fail on the other for reasons unrelated to the defense.

2. **No per-session token budget.** A user holding a long conversation can accumulate cost across turns. The per-turn cap helps but does not bound a session.

3. **No alerting when caps fire.** A regression that flips `tool_calls_limit` to 800 would not surface as a CI failure or a runtime alert. The cap is a silent ceiling.

4. **No regression case.** No hermetic eval case submits a runaway-tool-call payload and asserts the cap fires. The cap could be removed by a refactor and we would notice the next time someone reads the code, not the next time CI runs.

5. **Time-to-first-token not bounded by code.** The SSE stream has a connection-level timeout from the HTTP client side. If a turn takes 60 seconds, the browser may have dropped the connection before the rejection narrative emits.

## What a coherent policy looks like

The shape, not the specific numbers (those live in proto fields or a config file once we get there):

- Per-turn request cap. Hit point: model API calls per turn. Numerical value documented in one place; both runtimes read it.
- Per-turn tool-call cap. Hit point: number of primitive dispatches per turn. Same as above for documentation.
- Per-turn token budget. Hit point: sum of input + output tokens this turn. Less load-bearing than the others (the request cap dominates) but worth pinning.
- Per-session cost ceiling. Hit point: rough running-total cost across all turns in a thread.
- Time-to-completion cap. Hit point: wall-clock per turn.

Every cap stamps an OTel attribute on hit (something like `mcae.turn.cap_hit=tool_calls` plus the cap value), so eval probes can assert on a specific cap firing rather than just "the turn errored."

When a cap fires, the agent terminates the current turn cleanly with a refusal narrative (same shape as the topical-rail rejection narrative; see [chapter 02](02-user-input-topical-rail.md) for that pattern). The user sees a useful message, the trace records which cap fired, the eval probe verifies the cap fired.

## Subtle design point: the cap-hit narrative

A cap-hit refusal is different from a topical-rail refusal. The topical-rail refusal says "we did not run your turn because the input was unsafe." A cap-hit refusal says "we tried to run your turn and stopped because the work got too expensive." Both have the same wire shape (a narrative emit plus a turn-attribute stamp), but the user-facing wording should distinguish them. Mixing the two messages silently is a UX bug and obscures the trace.

This is one of the small things that gets dropped when caps are added in isolation per file. A unified policy treats the wording as part of the contract.

## Residuals

Two real ones.

- A cap that fires unconditionally on a malformed prompt produces a stream of failed turns and looks like a denial of service for the user. The cap behavior on the failure path (does the model retry, does the turn fully terminate, does the user get a useful message) matters as much as the cap value.
- A cap measured per turn does not protect against an attacker submitting many turns. Per-session caps are the next layer; rate-limiting at the HTTP boundary is the next-next layer.

## How we would prove it works

Unit tests on the cap constants are weak (they just check the constants exist). The behavioral pin should be an eval case:

- `evals/cases-hermetic/runaway_tool_call_loop.yaml`: a payload designed to trick the agent into a tool-call loop (the mock substrate can return a result that the agent thinks needs follow-up tool calls). Asserts `mcae.turn.cap_hit=tool_calls` plus `mcae.turn.tool_calls` clamped to the cap value. Runs under both runtimes; identical structural outcomes.

The case exists today only as a wish; tracked in a follow-up issue.

## Transferable summary

If you are building an LLM agent and have not unified your resource caps:

1. A per-turn request cap and a per-turn tool-call cap, with one numerical value per cap documented in one place and read by every runtime.
2. A per-session cost ceiling for long conversations.
3. A wall-clock cap per turn that the user-facing surface understands.
4. An OTel attribute stamp on every cap hit, so traces and eval probes can attribute the failure.
5. A refusal narrative whose wording distinguishes cap-hit from topical-rail rejection.
6. At least one eval case that submits an injection-shaped payload designed to blow past a specific cap, and asserts the cap fires.

Most teams build (1) through (3) piecemeal as the issue arises and never write down (4) through (6). The cost is silent: a regression removes a cap and nothing tells you until OpenRouter sends the bill.
