# 05: Build the ability to turn each defense off, and verify the runtimes are parallel

The lesson: if you have N defenses and you can only run "all on" or "all off," you cannot demonstrate which defense is doing the work on a given attack. Every defense needs an individual off-switch so eval cases can isolate the variable. Separately, if you have two runtimes (or two versions of one runtime) executing the agent loop, you cannot ship them confidently unless the defenses are bit-for-bit identical on the surfaces where drift matters. Both concerns are meta-architecture. They make the other defenses provable. They do not catch attacks directly.

This chapter is the one I see most teams skip. It feels like infrastructure rather than security. The cost of skipping shows up a year in, when no one can answer "is defense X still firing in production."

## Half 1: per-defense ablation

### The problem ablation solves

You wrote a prompt rule. You wrote a constitution-gate check. You wrote an envelope wrap. Each is supposed to catch some class of attack. How do you know they actually do?

The naive eval shape is: submit an attack, observe the model did not comply, declare the defense works. The problem is you do not know which defense did the work. The model might have refused on its own (no defense fired). The constitution gate might have retracted. The envelope wrap might have made the attack illegible. If you remove a defense and re-run, you can find out. Only if the defense is individually removable.

The defense surface needs a switch per defense, exposed on the request envelope so eval cases can flip them. The eval suite then has two kinds of cases: positive (all defenses on, attack fails to land) and negative (specific defense off, attack lands or behaves differently). The negative cases are the ones that prove each defense is doing what you think it is doing.

### The implementation

In our codebase: the `AgentSwitches.stay_in_role` sub-message in [switches.proto:66](../../proto/multichain/wire/agent/v1/switches.proto:66) is the per-defense ablation surface. One bool per defense:

```proto
message StayInRoleSwitches {
  bool defend_chat_template_spoofing = 1;
  bool defend_constitution_judge = 2;
  bool defend_persona_swap = 3;
  bool defend_decode_and_execute = 4;
  bool defend_identity_reveal = 5;
  bool defend_off_domain = 6;
}
```

Each flag gates either a prompt-rule drop or a code-path skip. The mapping lives in [composer.py::drops_from_switches](../../agent-service/src/agent_service/prompts/composer.py). The composer reads the switches and produces a set of rule IDs to drop from the assembled prompt. The wire layer reads other switches and skips screens (the chat-template rejection regex, for example).

Two design choices worth flagging.

**The mapping is not 1-to-1.** Some flags map to multiple drops. One drop is shared by two flags. The `defense:user_question_untrusted` rule's prose covers persona-swap and decode-and-execute framings in one tightly worded paragraph. We considered splitting, but the framing is shared. Either flag off keeps the rule. Only when both are off does the rule drop. The mapping is documented in the composer's docstring and tested in [test_prompts_composer.py](../../agent-service/tests/unit/test_prompts_composer.py).

**Some switches gate code, not prompt text.** `defend_constitution_judge` does not drop a prompt rule. It gates whether the constitution gate runs at all in the loop driver. With the judge off, gate spans do not fire and claims pass through without the LLM-as-judge stage. Eval cases that ablate this switch assert the gate spans are ABSENT from the trace (negative-path assertion) rather than present-with-different-content.

### What ablation cases look like

The canonical ablation case in our suite: [who_are_you_no_role.yaml](../../evals/cases-live/who_are_you_no_role.yaml). An identity probe with `defend_chat_template_spoofing=false` and `defend_constitution_judge=false`, all other defenses left on. The probes pin that the constitution-gate spans are ABSENT from the trace. If a future refactor accidentally always-runs the constitution gate (a safety regression in the other direction from "skipping it accidentally"), this case catches it.

The general pattern: positive cases verify "the defense fires when on." Negative cases verify "the defense does not fire when off." Both are needed. A defense that fires unconditionally is just as broken as one that does not fire at all, because it means the switch is not doing what the eval suite says it does.

### Production preset

Every flag in production is set to true. The composite "stayInRole" UI toggle that surfaces to operators flips the production preset on or off. The per-switch granularity is wire-only for evals and article-side experiments.

This is deliberate. End users do not need to think about which defense to enable. They get the production preset. Researchers and article authors get the granular surface for the article-side experiments that the security work is in service of.

## Half 2: runtime parity

### The problem

Two runtimes execute the agent loop today. Either can be selected at startup via an environment variable. The defenses must apply identically across them. Drift between the two would mean a deploy decision silently changes the security posture.

This is not a hypothetical concern. We observed real content drift between the two refusal narratives: the codex driver's refusal text was missing the analyst-agent suffix that the pydantic-ai driver's had. Both runtimes were "doing the screen," but they were doing it differently, and the difference was invisible until we read both implementations side by side.

The general lesson: any time you have N implementations of a defense, N grows the maintenance surface and shrinks the probability that all N stay aligned. Eliminating duplication is a security move, not just a code-quality move.

### Three high-risk drift surfaces

In our system:

1. The `<external_data>` envelope wire format. Three implementations (Python boundary, Rust MCP, hermetic mock). All must escape `<`/`>` identically. Drift means a payload that defeats the defense on one runtime passes on the other.
2. The user-input rejection narrative. Both runtimes emit a refusal frame when the chat-template screen fires. Drift in the narrative content means frontend rendering or logging differs by runtime.
3. The observability stamping on rejection. Both runtimes stamp the same `mcae.turn.unsafe_input_rejected=true` attribute plus the matched pattern. Drift means eval probes pass on one runtime and fail on the other for reasons unrelated to the defense.

### Mitigations

For the envelope wire format, the three implementations cross-reference each other in their docstrings, so a developer touching one is forced to touch the others. Each is pinned by a unit test that asserts the exact emitted byte string. Drift fails CI.

For the rejection narrative plus observability, we extracted the shared block into a single helper. Both drivers now call [core/run.py::emit_unsafe_input_rejection_observability](../../agent-service/src/agent_service/core/run.py). Runtime-specific concerns (how to emit a wire frame, how to persist thread state) stay in the driver. A constant `UNSAFE_USER_INPUT_REJECTION_NARRATIVE` is the single source of truth for the refusal wording.

The general pattern: where the implementations must be byte-identical, factor out the shared part as a helper or a constant. Where they have legitimately runtime-specific concerns (sink emission vs SSE frame yielding), keep those parts in the driver and pin the shared part separately.

### How parity is verified

Hermetic eval cases run under either runtime via the same environment switch. The hermetic substrate is identical across runtimes by construction (both runtimes hit the same `/eval/setup`-controlled mock). Probes that assert structural attributes (`mcae.turn.unsafe_input_rejected`, `mcae.turn.tool_calls`, narrative span presence) pin the rejection contract under either runtime.

[user_input_chat_template_rejection.yaml](../../evals/cases-hermetic/user_input_chat_template_rejection.yaml) is the canonical parity case: same payload, same probes, runs under both runtimes with byte-identical structural outcomes today.

Live cases tend to be runtime-specific because the live model's stochastic output differs by runtime. Cross-runtime parity at the LLM-output layer is not a goal. The defenses fire identically. The narrative wording is a model variable, not a defense variable.

## Residuals

Three to be honest about:

- Drift between the runtime defenses and the data plane. Our hermetic substrate snapshots the data plane's tool schemas into a checked-in JSON file. A Rust unit test fails any build where the snapshot drifts. That keeps the mock faithful. It does not catch drift in the defenses themselves. The per-implementation unit tests cover that.
- Drift between the agent-side ablation surface and any consumer that talks to the data plane directly. If a future internal tool talks to the data plane without going through the agent service, it bypasses the agent's defenses entirely. The defense rests on the deploy topology routing all user-question content through the agent layer first.
- Drift between the primary model and the policy model. Both are env-driven. A future config that points them at different providers can produce a primary that violates the policy the gate then retracts. The system handles this correctly (retract is the right verdict), but the article-side ablation has to choose models carefully to make the ablation interpretable.

## Why this chapter exists

The other four chapters describe defenses. This one describes the architecture that makes them provable.

The temptation to skip is real. The ablation switches add proto fields, eval-case complexity, and a switch-to-rule mapping that has to stay in sync as rules change. The runtime parity work is dedup that does not add features. Both are easy to deprioritize.

The cost shows up later. A team that ships defenses without ablation cases cannot answer "did this regression remove a defense or just change the model's behavior." A team that ships two runtimes without parity verification ships a deploy variable that silently changes their security posture. Both costs compound. They do not show up as a single incident. They show up as a slow loss of confidence in the system.

## Transferable summary

For ablation:

1. A switch per defense, exposed on the request envelope.
2. A mapping from switches to prompt-rule drops or code-path skips, documented and tested.
3. Eval cases that flip switches and assert the negative path (defense is ABSENT when off).
4. A production preset that runs with every defense on.

For runtime parity:

1. A list of surfaces where drift would matter (wire format, refusal text, observability).
2. Shared helpers or constants for the parts that must be byte-identical.
3. Cross-referencing docstrings on the parts that cannot be deduplicated, plus unit tests on each.
4. At least one eval case that runs under every runtime and asserts identical structural outcomes.

The deepest lesson of this chapter: defenses you cannot demonstrate are defenses you cannot trust. The infrastructure to demonstrate them is part of the defense, not a separate concern.
