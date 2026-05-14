# 03: Verify what the model emits

The lesson: even with input defenses fully wired, the model can fail honestly. It can fabricate a number, paraphrase a value wrong, drift off-domain, follow an instruction-shaped fragment in the data. The defense is a pipeline that runs on the model's output before it reaches the user. Cheap deterministic checks first, an LLM-as-judge last. Each stage catches what the next stage cannot.

This is what separates a production agent from a demo. Demos trust the model. Production verifies.

## The two output channels

Every turn the model emits two things.

**Structured claims.** Typed, with explicit provenance. In our codebase a claim has a `headline`, a `body_markdown`, and a `provenance` array listing every entity (wallets, communities, edges, time ranges, numbers) the claim references. Each number entry includes `metric` and `value`. Claims are optional per turn; the model decides whether the answer warrants one.

**Free-text narrative.** The model's response to the user. May reference earlier claims via placeholders like `${ref:N}`. Mandatory unless a channel switch suppresses it.

Splitting the output into structured and free-text is itself a defensive choice. Most production agents return a single blob of prose. Pulling out an audit-class structured channel means the verification pipeline can check the structured claims rigorously and check the prose against them. If everything were prose, you would have one fuzzy thing to verify and no anchor.

The verification rule we picked: every audit-class number in narrative or claim body must appear as a `${ref:N}` placeholder pointing at a provenance entry. Audit-class includes SOL volumes, edge counts, community IDs, anything a reader would treat as data. Descriptive numbers (years, "three points," "a single hub") pass through bare.

## The pipeline

Three deterministic checks plus one LLM-as-judge, running in series. A retract at any stage prevents the offending content from reaching the user. The model gets the verdict back and can re-emit a corrected version on the user's next turn.

```
Model emits Claim or Narrative
   │
   ▼
Stage 1: Placeholder gate
   │   ↳ every ${ref:N} resolves to a valid provenance index
   ▼
Stage 2: Structural verifier (binding store)
   │   ↳ every cited Number traces to a real primitive output
   ▼
Stage 3: Constitution gate (LLM-as-judge)
   │   ↳ identity, off-domain, citation-discipline, data-imperatives
   ▼
SSE wire to browser
```

Each stage is independently testable and independently version-pinned. Order matters: deterministic stages run first because they are cheap and authoritative. The LLM judge runs last because it is the expensive recall-based check that handles paraphrase and intent rather than syntactic structure.

## Stage 1: placeholder gate

A regex pass. Every `${ref:N}` token in the input must point at a valid index in the surrounding provenance array. The grammar is `\$\{ref:(\d+)\}`. First-error semantics (the gate reports the first invalid index and retracts).

The general lesson: if you have a citation discipline in your output, the cheapest defense is a syntactic check that citations resolve. Catches typos, off-by-one errors, and pure fabrication where the model wrote a placeholder pointing at an index that does not exist.

In our codebase: [agent-service/src/agent_service/policy/placeholder.py](../../agent-service/src/agent_service/policy/placeholder.py). Direct port from a Rust version (`backend/src/agent/policy_placeholder.rs`) because the same logic ran on the previous runtime; same regex grammar, same first-error semantics.

This stage cannot validate the values cited. Only that the indices are well-formed. Value validation is stage 2.

## Stage 2: structural verifier with a binding store

The defense against fabrication. Every successful primitive call records its returned values in a per-thread `PrimitiveBindingStore`. When the model emits a claim that cites a Number value, the structural verifier walks the provenance array and checks each entry traces back to a real binding-store entry.

The store is keyed by `(primitive_name, metric, value)`. A cited number that does not appear in the store retracts the claim. The retract reason names the specific provenance entry that did not trace, so the model's retry on the next turn has a concrete target.

This is the load-bearing anti-fabrication defense. A prompt rule that says "do not make up numbers" is a hopeful instruction. A deterministic check that every cited number was actually returned by a tool call is a contract.

In our codebase: [policy/structural.py](../../agent-service/src/agent_service/policy/structural.py) plus [policy/binding_store.py](../../agent-service/src/agent_service/policy/binding_store.py).

Two intentional short-circuits worth knowing:

- **Raw-class numbers** (years, fractions without context, embedded digits in addresses) skip the structural check because they do not need provenance. The classifier in [policy/crosscheck.py](../../agent-service/src/agent_service/policy/crosscheck.py) assigns a unit class. `Raw` is the pass-through bucket.
- **Edge and TimeRange entities** skip the value check because their identity is structural (a pair of wallet addresses, a time interval) rather than a single number. The provenance contract still requires the entry; only the value compare does not apply.

The general pattern: a structural verifier needs a typed value lattice so it knows what counts as "the same value" for each class. Reusing the model's own typing here (wallet, edge, community, time range, number) gives the verifier a finite enumeration to dispatch on.

## Stage 3: constitution gate (LLM-as-judge)

A cheap-model LLM judge that reads the output and returns `approve` or `retract` with a one-sentence reason. The judge prompt enforces rules the deterministic stages cannot:

- **Provenance non-empty.** A claim with empty provenance retracts.
- **No imperatives lifted from data.** The agent reads on-chain strings inside `<external_data>` blocks (see [chapter 01](01-external-data-envelope.md)). The output must not echo any instruction-shaped phrasing from those blocks back as commands or suggestions.
- **Domain.** Writing code, financial advice, predictions, off-topic content all retract. A polite domain refusal is in-domain.
- **Identity.** Can identify as the analyst agent. Cannot name the underlying LLM. Cannot claim sentience or capabilities it does not have.
- **Citation discipline.** Every audit-class number in prose must appear as `${ref:N}`. The judge handles intent: "the wallet has 3 distinguishing properties" approves (descriptive); "the wallet has 33 connections" retracts unless cited.
- **No identity guessing.** Narrative cannot name real-world entities ("this is Binance") unless a tag-source primitive provides the label.

In our codebase: [policy/constitution.py](../../agent-service/src/agent_service/policy/constitution.py). The judge's prompt lives in [policy_v4.txt](../../agent-service/src/agent_service/prompts/policy_v4.txt).

The judge also emits a sidecar: a list of numeric quantities it sees in the narrative, classified by unit. The runtime uses this as a coherence advisory in a cross-check sub-mode; it does not drive the wire verdict. The sidecar is useful for debugging recall regressions ("the judge approved but did not see the number we cared about") without coupling it to the strict-merge decision.

A design choice worth flagging: borderline-leans-approve. Clear violations retract. Stylistic preferences do not. The structural stages run alongside and catch unsourced chip values deterministically, so the judge's job is the imperatives, identity, and domain axes the deterministic stages cannot. If you make the judge too strict, you get false retracts on legitimate borderline phrasing, and the system stalls.

## Stage 0: the prompt-side framing that makes verification possible

Not a stage in the pipeline, but worth calling out. The model has to know the verification exists, otherwise it produces output that fails the pipeline every turn. Our system prompt teaches the citation discipline ("every audit-class number must be a `${ref:N}` chip"), the provenance contract ("every claim must reference an entity"), and the cross-check expectation ("we will retract if your prose disagrees with your chips").

Partly redundant with the gates (the model could ignore the prompt and the gates would still retract), but the redundancy is worth the prompt tokens. A model that understands the contract emits fewer retract-bait outputs, which means fewer retry round-trips, which means lower latency and lower cost per turn.

The companion switch is `dontFabricate`. The switch does not gate a specific check. It gates whether the "do not make up numbers" framing appears in the assembled prompt. Switching it off in an article ablation lets researchers observe what the model fabricates without the framing. The binding-store still records what was actually called, so the structural verifier still retracts fabricated values regardless. The switch removes the model's warning, not the check.

## Residuals

Two real gaps to be honest about:

- Narrative truthfulness about non-numeric content. The structural verifier checks values. A claim that paraphrases a tool result inaccurately ("the wallet is suspicious" when the data says nothing about suspicion) passes placeholder and structural, retracts at the constitution gate if the judge catches it. The judge's recall on such cases is the weakest link. Eval cases with adversarial paraphrase are the way to surface gaps.
- Cross-claim arithmetic. Our prompt forbids the model from computing sums, ratios, or percentages over Claims. The constitution gate is supposed to catch violations. Neither the placeholder gate nor the structural verifier knows arithmetic. A model that emits a fabricated sum cited as `${ref:N}` passes structural (if the cited entry exists) and leans on the judge.

The pipeline is good at numbers-without-provenance. Weaker at prose-with-wrong-interpretation. When you build your own, the same trade-off applies. Deterministic checks catch what they can check. The judge catches everything else. An honest accounting of what neither catches is the start of your eval suite.

## Why three deterministic stages plus one LLM stage

The split is not accidental. Deterministic stages are cheap, fast, authoritative. They cannot be paraphrased around. They only catch what they can check syntactically: that a `${ref:N}` resolves, that a cited Number value matches a binding. They cannot catch "the agent's prose claimed this wallet is a phishing operation" because there is no structural marker for an unsupported identity claim.

The LLM judge handles the recall axis: reads the prose and flags the cases the deterministic stages cannot. The trade-off is cost (one extra LLM call per turn) and flake (a cheap model can disagree with itself across runs). The current design accepts the cost because the alternative (letting prose attacks through) is worse, and the flake is bounded by leaning-approve on borderlines plus the deterministic stages catching the structural violations regardless.

## How we proved it works

Eval cases that exercise the pipeline:

- Every hermetic case under [evals/cases-hermetic/](../../evals/cases-hermetic/) flows through it. The impostor-mint cases pin that the token-verification rule fires on `verified=false` payloads.
- [refusal_smoke.yaml](../../evals/cases-live/refusal_smoke.yaml): off-domain question. Pins Rule 3 retract.
- [wallet_profile_smoke.yaml](../../evals/cases-live/wallet_profile_smoke.yaml): pins Rule 5 (citation discipline) on real wallet data.
- [wallet_profile_fabrication_allowed.yaml](../../evals/cases-live/wallet_profile_fabrication_allowed.yaml): article-side ablation that disables the `dontFabricate` framing and observes what the model fabricates. The structural verifier still retracts. The eval measures how often.

## Transferable summary

If your agent emits anything a user will act on:

1. A structured channel separate from the prose, with explicit provenance for cited values.
2. A syntactic check that citation indices resolve (cheapest).
3. A semantic check that cited values trace to real tool calls (load-bearing anti-fabrication).
4. An LLM judge that handles intent and paraphrase. Cost the extra inference call; the alternative is letting prose attacks through.
5. An honest accounting of what (4) does and does not catch reliably, kept up to date in your eval suite.

The structured channel is the move most agents skip. Without it, (3) is impossible because there is nothing to compare prose against.
