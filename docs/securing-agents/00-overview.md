# 00: Securing agents, overview

Notes on building LLM agents that have to read attacker-controllable data and emit grounded output. Each chapter is one lesson worth transferring; the running example is our Solana transaction-graph analyst, with actual file paths and tests so you can see how the principle landed in real code.

## The setup that creates the problem

An agent is an LLM with tools. The LLM plans, the tools fetch data or take actions, and the data the tools return becomes part of the LLM's next prompt. The product is what the agent emits to the end user.

Three things flow into the model on every turn:

1. The system prompt. You wrote it. Trusted.
2. User-controlled text: the question, the chat message, the email body. Untrusted because the user might be hostile.
3. Data the tools fetched: web pages, database rows, on-chain strings, file contents. Untrusted, and the population of potential attackers is everyone who can write to that data source.

The first is fine. The second and third are where things get hard, and they are different problems with different fixes. Most agent failures I have read about come from treating them as one problem ("how do I prevent prompt injection") rather than two.

In our codebase: the user types a wallet-analysis question. The agent calls `wallet_profile`, `community_summary`, or `get_token_info`. The token-info tool returns strings the mint authority chose at token creation, including the `name` field, which is literally a Token-2022 attribute set to whatever bytes the issuer wants. Same shape as any retrieval-augmented agent: untrusted user input on one side, untrusted retrieved data on the other.

## The lesson behind each chapter

[01: Wrap untrusted data, then escape the wrapper](01-external-data-envelope.md). When the LLM reads data the agent fetched, mark the boundary in the prompt with a structural envelope and treat what's inside as opaque. Then close the hole: an attacker who can write the close tag into the data can forge the envelope's end. Escape the bracket characters so the only literal close tag in the prompt is yours.

[02: Two layers for user input](02-user-input-topical-rail.md). Reject what shouldn't exist (chat-template control tokens), escape what could be ambiguous (markup the user could plausibly type that also matches your operator-side syntax), and instruct the model around the rest (plain-English social engineering reaches the model and falls to a prompt rule plus output verification).

[03: Verify what the model emits](03-output-verification-pipeline.md). The model fails honestly: fabricates a number, paraphrases a value wrong, drifts off-domain. Run a pipeline on the output. Cheap deterministic check (do citations resolve), then a semantic deterministic check (do cited values trace to real tool calls), then an LLM-as-judge for the cases syntax cannot catch. Order matters because the cheap ones are authoritative.

[04: Pin the agent's domain and identity at two layers](04-domain-and-identity-discipline.md). Off-domain refusal and identity discipline are model-layer concerns (a prompt rule) AND output-layer concerns (a constitution gate that retracts violations). Either alone is fragile. For domain-specific impersonation (in our case fake-USDC tokens), add a small registry of canonical things you stand behind and let the model see the verification flag.

[05: Build the ability to turn each defense off, and verify the runtimes are parallel](05-per-defense-ablation-and-runtime-parity.md). The other four chapters describe defenses with measurable behavior. If you cannot disable defense X individually and re-run your eval suite, you cannot demonstrate which defense is doing the work. If you have two runtimes executing the same agent loop, you cannot ship them unless their defenses are bit-for-bit identical on the surfaces where drift matters.

[06: Resource bounds as a defense](06-resource-bounds.md). A prompt-injected agent can fail in expensive ways: tool-call loops, token-burn injections, quota-exhaustion via legitimate-looking traffic. Most of these will not show up in your correctness-focused eval suite. You need explicit caps on turns, tool calls, tokens, and time, with at least one regression case that proves the cap fires when an attacker tries to blow past it. Our caps today are scattered across two runtimes and one thread-state pruner with no unified policy; this chapter is the audit.

[07: The meta-defenses are not free](07-meta-defense-trust-boundary.md). Every defense you build with an LLM is itself attackable by an LLM-shaped attack. The constitution gate is an LLM. The judge reads text the user influenced. The same prompt-injection techniques that work on the primary work on the judge, sometimes more easily because the judge runs on a cheaper model. If you treat the meta-defenses as trusted infrastructure, you have a blind spot. This chapter is the threat model for the pipeline you built in chapter 03.

## The high-level model

Every prompt-injection defense answers one of three questions: did the bytes get in, what does the model think they are, what is the model allowed to say in response. A complete posture answers all three.

| Question | Defense direction | Example layer |
|---|---|---|
| Did the bytes get in? | Wire layer: reject, escape, or sanitize before the model sees the input. | Topical-rail rejection of chat-template tokens. Unicode-escape of brackets in user and tool-result slots. |
| What does the model think they are? | Prompt layer: rules that frame untrusted regions. | `<external_data>` envelope with a rule that explains what's inside. `defense:user_question_untrusted` rule for the question slot. |
| What is the model allowed to say in response? | Output layer: deterministic checks plus an LLM judge on the emitted content. | Placeholder gate, structural verifier against a binding store, constitution gate. |

The chapters roughly follow that order. 01 and 02 cover the two input slots at the wire-and-prompt layer. 03 covers the output layer. 04 spans prompt and output for domain and identity. 05 is the meta-architecture that lets you prove the others work. 06 covers what the eval suite is unlikely to catch: the cost shape of an injected agent. 07 turns the same defenses on themselves and asks whether the verification pipeline has its own attack surface.

## What the running example is

The Solana analyst agent lives in this repo. It uses two runtimes (a `pydantic-ai` HTTP path and a `codex` MCP path), so most defenses appear in two implementations. Where the implementations matter for the lesson, both file pointers are given. Otherwise we link to whichever reads more clearly.

Key entry points if you want to read along:

- [agent-service/src/agent_service/boundary.py](../../agent-service/src/agent_service/boundary.py): the trusted/untrusted boundary helpers (envelope wrapping, user-input rejection, the user-question escape).
- [agent-service/src/agent_service/prompts/system_v4.txt](../../agent-service/src/agent_service/prompts/system_v4.txt): the model-layer rules.
- [agent-service/src/agent_service/policy/](../../agent-service/src/agent_service/policy/): the output verification pipeline (placeholder, structural, constitution, binding store).
- [backend/src/mcp.rs](../../backend/src/mcp.rs): the Rust side of the MCP envelope.
- [evals/cases-hermetic/](../../evals/cases-hermetic/): hermetic eval cases that pin each defense. Every chapter names the specific case that proves the defense fires.

## What this folder is NOT

A defense survey. Good ones exist already: OWASP LLM Top 10, the vendor guidance from Anthropic / OpenAI / Microsoft, and the Greshake et al. paper on indirect injection. This folder is the practitioner's-notebook version. Which defenses we ended up using, why, what they catch in production.

A substitute for adversarial review. The eval cases pin known attacks. They cannot rule out unknown ones. Every chapter calls out residual surface area so it stays visible.

Authentication or rate-limiting. Infrastructure concerns one layer below the agent loop. When auth lands in this codebase, the folder grows a chapter on session isolation.

## A note on layering

The most common shape of agent-security failure I see is "we added defense X, so we are good." X is usually a single prompt rule or a single regex. Every chapter here has the same shape: one prompt-layer thing plus one wire-layer thing plus (where applicable) one output-layer thing, with each able to fail without the others noticing.

When you build your own, the question to ask at each defensive surface is: what catches this attack if the layer I am about to add fails. If the answer is "nothing else," you have one layer of defense, and you should add another before shipping.
