
# Frontier eval review: where we sit, May 2026

## The frontier as of May 2026

Industry consensus on what a serious agent eval substrate needs (verified dated sources at end):

1. **OTel-as-backbone** for traces (vendor-agnostic, semconv work ongoing)
2. **Three-layer eval architecture**: unit evals on discrete steps + LLM-as-judge regression suites + continuous production trace sampling
3. **Trajectory-aware** (not just outcome): assert on intermediate steps, gate verdicts, tool sequences
4. **Scaffold-vs-model decomposition**: evals measure model × scaffold × token-budget product
5. **Counterfactual / scaffold ablation**: toggle a switch, observe trajectory diff
6. **Production-driven case generation**: capture surprising traces, freeze as evals
7. **Multi-turn / long-horizon**: conversation-level metrics (knowledge retention, context drift, role inconsistency)
8. **Adversarial robustness**: direct + indirect prompt injection, goal hijacking, tool misuse, context window pollution
9. **Inconclusive vs failure**: distinguish provider errors from model regressions
10. **Preference-leakage prevention**: judge family ≠ model-under-test family
11. **Cost/latency as first-class metrics**

## Where ours is frontier-grade

| Dimension | Our shipped position |
|---|---|
| OTel backbone | Cross-process pipeline (agent → otel-collector → CH + Langfuse). Span-name + attribute conventions namespaced under `mcae.*` as a stable contract. |
| Trajectory-aware probes | Per-claim `mcae.claim.emitted` spans, gate verdicts, claim grounding, structural retraction visibility. We probe intermediate steps, not just narrative output. |
| Scaffold/model decomposition | Env-driven model ids, `RegressionReport.model_deltas` calls out swaps, baseline pins `agent_primary_model` / `agent_policy_model` / `eval_judge_model`. |
| LLM-as-judge regression | `llm_judge` probe with rubric, target_attrs (trajectory or outcome mode), pass_threshold. |
| Inconclusive vs failure | `infra_health.has_terminal_provider_failure` flips probes to `inconclusive=True` rather than fail; baseline diff skips inconclusive. Aligned with [Promptfoo's ERROR-vs-FAIL state](https://github.com/promptfoo/promptfoo) (the model we targeted in earlier design). |
| Preference-leakage prevention | Env-derived forbidden families on judge model. ICLR 2026 work was the cited motivation; we ship the prevention. |
| Counterfactual / scaffold ablation | Switch-off cases (`who_are_you_no_role`, `wallet_profile_fabrication_allowed`). Industry name: **scaffold ablation**. We have it at single-trace granularity. |
| Provider robustness | Per-attempt timeout + retry (this session), elapsed-time logging per call, slowest-call diagnostic probe. |
| Refusal discipline | `refusal_smoke` (off-domain) + `refusal_prompt_injection` (this session): assert text-vs-tool stays aligned ([Agent-SafetyBench Dec 2025](https://arxiv.org/abs/2412.14470) framing). |

**Honest read**: this is a frontier-grade foundation for the categories it covers. The substrate's quality matches what the dated 2026 industry literature describes.

## Where ours has the bones but is shallow

| Dimension | What we have | What's shallow |
|---|---|---|
| **Counterfactual / scaffold ablation** | Single-switch toggling per case | No cross-trace differential probe (gated vs ungated on same input). Filed as #29  that's the right ticket. |
| **Tool misuse coverage (OWASP ASI02)** | Refusal cases pin `tool_calls=0` on off-domain | No test of "wrong tool fired" (e.g., asked wallet question, agent fired community_summary). Could add via `tool_called_with_args` negative assertions, but value is low at our 2-primitive surface. |
| **Cost/latency metrics** | `span_latency_p50_under`, `slowest_call_under_ms` | No token-cost or token-count probe. OTel emits `gen_ai.usage.input_tokens` / `output_tokens` already; one new probe shape covers it. Value when we cost-tune. |
| **Tool argument validation** | `tool_called_with_args` (top-level keys only) | Doesn't read nested JSON paths. Already noted in `wallet_profile_smoke.yaml` comments as a future probe extension. |
| **Adversarial robustness** | One canonical prompt-injection vector (`refusal_prompt_injection`) | One vector ≠ coverage. AgentDojo (UK AISI's framework) tests dozens; we test one. Worth widening incrementally. |

## Where ours is genuinely missing

| Gap | Recommendation |
|---|---|
| **Multi-turn / long-horizon eval.** ThreadRegistry exists (Ship 4 prep) but zero multi-turn eval cases. Confident AI's 2026-03-22 post names knowledge retention, context drift, role inconsistency as the dominant failure modes; we test none. | **File ticket. Real gap.** Defer until ship 4 lands  testing multi-turn before multi-turn ships is YAGNI. |
| **Indirect prompt injection** (instructions hidden in primitive output). NVIDIA's 2026 blog and AgentDojo flag this as the dominant 2026 attack vector. Our primitive outputs are structured JSON from our own Rust service today, so the surface is small  but the in-flight token metadata pipeline opens it (Metaplex name / symbol / uri and the off-chain JSON those uris point to are user-authored text). | **File deferred ticket.** Not worth covering until the metadata pipeline is end-to-end and the agent has a primitive that surfaces metadata text. |
| **Goal hijacking (OWASP ASI01).** Multi-turn variant of injection: a session starts benign, later turns subtly redirect the agent. Our prompt-injection case is single-turn. | **Bundle into the multi-turn ticket above.** Same eval shape, same blocker (need multi-turn eval first). |
| **Production trace sampling.** Anthropic's 2026-01-09 piece: layer 3 of the eval architecture. We have CH with `mcae.run.type` discriminator, so production traces ARE captured  we just don't sample them into eval. | **File deferred ticket.** Real value when production traffic exists. Today everything in CH is dev/eval. |
| **Trace-replay case generation.** Already filed as #30 (deferred). Closes the loop with #28 supersession reasoning. | **Already filed.** No action. |
| **Cross-trace differential probes.** Already mentioned in #29 as out-of-scope-for-now. | **Already flagged.** No action. |
| **Context window pollution / latent injection.** Multi-turn problem: an early turn plants instructions that activate later when context shifts. | **Bundle into multi-turn ticket.** |

## Summary

**Aligned with the May 2026 frontier on**: substrate architecture, layered probe types, trajectory-awareness, scaffold/model decomposition, judge bias prevention, provider robustness, single-trace counterfactuals, refusal discipline.

**Genuine gap, action: file**: multi-turn eval (deferred until Ship 4 lands), indirect prompt injection (deferred until we ingest external content), production trace sampling (deferred until production traffic).

**Already filed**: trace-replay generation (#30), cross-trace differential probes (#29).

**Not worth chasing**: synthetic case generation (frontier moved past it), exhaustive injection vector library at the substrate level (better as a red-team subsuite when warranted).

The work this session ships a substrate at frontier quality for what it covers, with the honest gaps clustered around capabilities we haven't built yet (multi-turn) or surfaces we don't yet touch (untrusted content, production traffic).


Sources (dates verified per AGENTS.md rule):
- [HuggingFace: AI evals are becoming the new compute bottleneck (2026-04-29)](https://huggingface.co/blog/evaleval/eval-costs-bottleneck)
- [Anthropic: Demystifying evals for AI agents (2026-01-09)](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
- [Confident AI: Multi-Turn LLM Evaluation in 2026 (2026-03-22)](https://www.confident-ai.com/blog/multi-turn-llm-evaluation-in-2026)
- [Agent-SafetyBench (arxiv 2412.14470, Dec 2025)](https://arxiv.org/abs/2412.14470)
- [OWASP Top 10 for Agents 2026 (DeepTeam framing)](https://www.trydeepteam.com/docs/frameworks-owasp-top-10-for-agentic-applications)
- [AgentDojo (UK AISI inspect_evals)](https://ukgovernmentbeis.github.io/inspect_evals/evals/safeguards/agentdojo/)  date not verifiable on the fetched page; treat as a known framework, not a dated claim
- [OpenTelemetry blog: AI Agent Observability](https://opentelemetry.io/blog/2025/ai-agent-observability/)  URL dated 2025; cited only as evidence the OTel semconv work is live, not as a fresh source
- [Promptfoo (red-teaming framework)](https://github.com/promptfoo/promptfoo)  used by OpenAI and Anthropic; project page not date-verifiable in this fetch