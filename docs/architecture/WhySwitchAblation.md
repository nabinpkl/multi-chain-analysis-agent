# Why switch ablation

The agent has roughly a dozen defenses. The constitution gate, the binding-store value compare, the placeholder gate, four `defense:*` prompt rules, the `<external_data>` envelope, the user-question topical rail, the canonical-mint stamp, and a few channel toggles. Each is supposed to catch something. The question this doc answers is why every one of them has an individual off-switch, exposed on the wire, rather than a single "production preset" toggle.

For the per-switch implementation map (which file realizes which contract), see ADR 11 at [architecture-decisions/11-agent-switches.md](../../architecture-decisions/11-agent-switches.md). For the security perspective on what ablation enables, see [docs/securing-agents/05-per-defense-ablation-and-runtime-parity.md](../securing-agents/05-per-defense-ablation-and-runtime-parity.md). This doc is the architectural rationale.

## The decision

Every gate, prompt rule, and channel-redaction layer is wired through a dedicated bool on the `AgentSwitches` proto. The boolset is part of the wire request; eval cases, article-side experiments, and the builder-view UI panel all flip switches by setting fields on the request envelope. There is no global "demo mode" flag and no per-environment defense disable.

Default for every production turn: all switches on. The composite production preset is a wire-side concept (the request envelope ships with every flag at `true`), not a server-side flag. Disabling a defense always happens by sending an explicit `false` from the caller.

## What this design buys

**Eval probes can attribute failure to a specific defense.** If you submit an attack with all defenses on and the model refuses, you do not know which defense made the difference. The model might have refused on its own; the constitution gate might have retracted; the envelope wrap might have made the attack illegible. Per-switch ablation gives the eval suite a way to isolate the variable: a case with `defend_chat_template_spoofing=false` and everything else on tests one defense in isolation. Same payload, same model, same prompt scaffolding; only the wire-layer screen is missing. The result delta is the defense's contribution.

**The article surface is honest about what each defense does.** The agent runs as a builder portfolio piece. The intended reader is technical, and the value proposition is "see exactly which guard prevents which failure." That requires being able to flip guards individually and replay the same input. A single "stay in role" toggle that flipped four prompt rules and two code-path skips at once would let the reader see "with this on, the agent behaves; with this off, it does not", but would hide what each underlying rule contributes. The per-switch surface keeps every rule's contribution legible.

**Future ships strengthen existing switches rather than spawning new ones.** The switch is the API surface. The implementation map (per-switch) grows. When ship 5a added citation discipline, it folded into `stay_in_role` as another constitution-gate rule, not as a new switch. When envelope unicode-escaping landed in 2026-05, it folded into the existing `<external_data>` envelope behavior under the same code path that already existed. The UI panel did not grow a row. The wire surface did not grow a field. The number of switches is bounded by the number of distinct behavior contracts, not by the number of mechanisms that realize them.

**Cross-runtime parity is testable.** Each switch lives on the wire. Both the pydantic-ai loop and the codex driver read the same `AgentSwitches` proto. A hermetic eval case can run under either runtime with the same switch payload and assert identical structural attributes. Without the per-switch wire surface, the two runtimes would each have to ship their own ablation surface, and the two surfaces would drift; with one shared wire envelope, parity is a one-line probe.

## What this design costs

**Switch-to-rule mapping has to stay in sync.** The composer reads switch booleans and produces a set of `defense:*` rule IDs to drop from the assembled prompt. Some flags map 1-to-1; one rule is shared by two flags (the `defense:user_question_untrusted` rule covers persona-swap AND decode-and-execute framings as one paragraph, so either-on keeps the rule, both-off drops it). The not-quite-1-to-1 mapping is documented in `composer.py::drops_from_switches` and tested explicitly. A new prompt rule has to remember to wire its drop here; adding a rule without the drop is a real-world drift hazard, caught by a "drops match known rule ids" test in the composer suite.

**The proto field count grows with each behavior contract.** Today the `StayInRoleSwitches` sub-message has six fields. Each new defense behavior is a wire change plus a regen plus a composer-map update. The cost is real but bounded by the rate at which new behavior contracts emerge (slow; under one per ship in practice).

**End-user surface area is bigger than it needs to be.** A casual visitor does not need to see twelve toggles. Production routes them through a composite "production preset" UI control that flips the production-shape bundle as one operation. The per-switch granularity is wire-only and surfaces under the builder-view toggle for the article-side surface. End-user complexity stays low; expert complexity stays accessible.

## The alternative we rejected

**One global "demo mode" with no defense ablation.** Cheaper to build, smaller wire surface, smaller UI. Rejected because the project is a builder portfolio piece and the article-side experiments require granular attribution. Without per-switch ablation, the agent's defense story is "we have layers and they work"; with it, the story is "here's exactly which layer catches which failure, run it yourself." The article only earns reader trust on the second version.

**Per-defense flags configured server-side via env vars.** Switches not on the wire; the operator sets which defenses are on at boot. Rejected because eval cases need per-case ablation (different case wants different switches) and a single env-var configuration makes that impossible without restarting between cases. Wire-side switches let every case carry its own switch payload.

## The contract with future ships

The switch is durable. Future ships:

1. Strengthen existing behaviors under existing switches when the new behavior fits the same contract (citation discipline under `stay_in_role`, envelope-escape under the existing `<external_data>` flow).
2. Add a new switch only when a genuinely new behavior class appears (a class no existing switch's prose covers).
3. Update the composer's switch-to-rule map in the same commit as any change that adds or renames a `defense:*` prompt rule.
4. Add at least one ablation eval case for any new switch: a positive case (switch on, expected behavior fires) and a negative case (switch off, expected behavior does not fire).

The ADR's implementation map is what tracks which file realizes which contract today. The map updates as the codebase moves (Rust to Python migration, library swaps, refactors); the switch surface itself stays stable.
