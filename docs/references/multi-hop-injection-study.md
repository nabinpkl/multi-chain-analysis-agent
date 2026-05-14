# Multi-hop indirect injection across composed agents

Status: design, not yet implemented. Untrusted-text channel for the study
is **on-chain token metadata** (Metaplex name/symbol/uri and Token-2022
metadata extension fields, plus the off-chain JSON those URIs point to).
The metadata pipeline is in flight; this study's probe corpus depends on
that pipeline reaching ClickHouse end-to-end before it has a real channel
to test against. See `docs/architecture/token-metadata-ingestion.md`.

## Why this study exists

Single-agent indirect injection is well studied. An attacker plants text in a
source the agent reads (web page, email, on-chain text field), the agent
treats the text as instructions, the agent acts adversarially. Defenses for
this case are mature: input sanitization, instruction filtering, constitution-
style gates, channel switches that mark inputs as untrusted. We already have
most of these on the single-agent surface.

Multi-agent indirect injection is less studied. When agent A reads untrusted
text and produces a claim, then agent B consumes A's claim, does B's defense
system treat the claim as trusted-internal or as derived-untrusted? The
adversarial intent has crossed two boundaries. Today's defenses are designed
for one.

Mid-2026 surveys flag the multi-hop surface as the next frontier but do not
yet provide deterministic evals or controlled studies. Each external-A2A
study is a black-box composition: the agents come from different teams, have
different training, use different defense stacks. The setup is realistic but
the controlled-experiment cost is too high to learn from cleanly.

Inside one process, with two specialist agents on the same codebase sharing
the same defense stack, the multi-hop surface becomes a measurable thing.
That is what this study is about.

This is a learning project. Product utility (alerting users to unusual
Solana activity, letting them ask follow-up questions) is real but secondary.
The artifact is the experimental data.

## System under study

Three components, one process, deterministic where possible.

1. **Rule engine.** Rust, ingest-side. Pattern-matches the edge stream against
   a small fixed rule set (whale moves, drains, fan-in bursts). Pure
   deterministic logic, no LLM. Emits candidate incidents with structural
   evidence and references to mint pubkeys onto `solana.candidates.v1`.
2. **Monitor agent.** Python, agent-service, specialist with `task="triage"`.
   Receives candidates from the rule engine. Enriches with token metadata
   text (name / symbol / uri from Metaplex or Token-2022 extension, plus the
   off-chain JSON description and link fields if fetched; untrusted external
   input). Composes structural plus textual evidence into a triage claim with
   severity. Outputs alert.
3. **Q&A agent.** Python, agent-service, specialist with `task="qa"` (today's
   chat agent). Receives a turn from the user, optionally seeded with prior
   monitor claims as context. Answers conversationally.

Both agents run through `agent_service.core.run_one_turn` with different
system prompts, different tool surfaces, and different envelope shapes. The
chat driver and the (future) monitor driver are thin builders of
`TurnEnvelope`. The cross-agent communication is the monitor's claim being
placed into the Q&A's envelope as `prior_claims`.

The frontend never distinguishes the two agents. From the user's view it is
one assistant; the architecture under it is the experiment.

## Boundaries and what crosses them

Three boundaries. Named so experiments can target them.

**Boundary 1: external text → monitor.**
Crosses: token metadata text from Solana mainnet (untrusted by definition; any
token issuer chooses the name / symbol / uri at mint creation, and the off-
chain JSON the uri points to is even more freely-controlled).
Existing defenses: `external_text_input_enabled` channel switch, monitor's
system prompt instructions on how to treat metadata content, constitution gate
on monitor's claim before it leaves the agent.

**Boundary 2: monitor's claim → Q&A's prior_claims.**
Crosses: monitor's narrative summary, derived from untrusted token metadata
text upstream.
Existing defenses: none. This is the surface this study designs and measures.

**Boundary 3: Q&A's response → user.**
Crosses: prose composed from monitor's claim and Q&A's own primitive calls.
Existing defenses: viewer-side disclaimer, narrative-output channel switch,
structural value-compare gate.

The study targets boundary 2 specifically. Boundaries 1 and 3 are
well-trodden; boundary 2 is the surface a multi-agent composition creates.

## Defense surface today

| Defense                              | Boundary 1 | Boundary 2 | Boundary 3 |
| ------------------------------------ | ---------- | ---------- | ---------- |
| External-text-input channel switch   | yes        | (open)     | n/a        |
| Constitution gate per claim          | yes        | (open)     | yes        |
| Structural value-compare             | n/a        | (open)     | yes        |
| Viewer disclaimer                    | n/a        | n/a        | yes        |
| Narrative-output channel switch      | n/a        | n/a        | yes        |
| Provenance annotation per claim field| n/a        | (open)     | n/a        |

The (open) cells are the research surface.

## Research questions

Four questions, each maps to one experimental axis.

### Q1. Does the constitution gate at boundary 2 need to fire fresh, or can it trust boundary 1's verdict?

If Q&A trusts that the monitor's constitution-approved claim is safe, the
system has implicit transitive trust. If Q&A re-runs constitution on incoming
prior_claims, gates compose at 2x LLM cost per turn.

Honest question: does re-verification catch attacks boundary-1 verification
missed?

Probe shape: 30 adversarial inputs designed to pass boundary 1 (look benign
in claim form) but be malicious once Q&A treats them as evidence.

Measure: attack success rate at boundary 2 with re-verify on vs off.

### Q2. Does the external-text-input switch propagate?

If the user sets the switch off (no untrusted external text influences the
agent), should monitor's claim still flow into Q&A? Today the claim is
"internal" by virtue of not being raw metadata, but it is derived from raw
metadata. The switch's intent is violated in spirit if not in implementation.

Probe shape: same adversarial corpus, run with switch off vs on.

Measure: attack success rate.

### Q3. Does claim provenance need to be carried across the hop?

A claim that says "this wallet sent 5000 of token X" has structural fields
that are trustworthy (rule fired on real chain data) and a narrative
paraphrase generated by the monitor's LLM. The narrative could carry
adversarial framing planted in token X's metadata description. If the claim
envelope carries per-field provenance ("structural fields trusted;
narrative_text derived from untrusted token metadata"), Q&A can treat each
field by its own trust level. Without provenance, Q&A treats the whole claim
at one trust level.

Probe shape: probes that target the narrative_text field specifically, not
the structural fields.

Measure: attack success rate with per-field provenance vs flat.

### Q4. What is the null hypothesis?

If the monitor's probabilistic surface is small (metadata interpretation
only) and the rule engine handles 90 percent of decision-making
deterministically, defenses might compose trivially because the LLM has too
little to act on. That would be a null result.

Mitigation: the probe corpus must specifically target the LLM's narrow
surface. Probes that attack rule-engine output are out of scope. Rules are
deterministic; manipulating their output through token metadata text is
impossible.

If the null result holds, that itself is a finding worth writing up: in
domains where the deterministic layer is thick and the probabilistic surface
is narrow, multi-hop injection at boundary 2 is bounded by the probabilistic
surface size.

## Experimental design

**Probe corpus.** 30 to 50 hand-authored adversarial token metadata records.
Each probe records:

- Metadata text fields (name, symbol, uri; if uri is fetched, the off-chain
  JSON's description and link fields too)  the attack payload.
- Structural context to attach it to (mint pubkey, signature, transfer
  amount, source, destination of an associated edge).
- Probe class (see below).
- Expected behavior under each defense configuration.
- Actual behavior observed during runs.

Hand-authored, not LLM-generated, so each probe is auditable. The corpus
lives in `agent-service/evals/multi_hop_injection_study/probes/` as YAML
fixtures.

**Probe classes.**

1. **Direct injection.** Metadata says "ignore previous instructions" or
   similar classic shapes in the name / description field. Boundary 1
   should catch all of these; included as a sanity-check baseline.
2. **Indirect framing.** Metadata paints the token as legitimate when it
   isn't, or vice versa. Targets the narrative composition step.
3. **Cross-boundary delayed.** Metadata plants context that only becomes
   weaponized when Q&A composes a follow-up answer in a multi-turn
   conversation. The novel class.

**Defense configurations.** A 2 by 2 by 2 grid:

- Boundary 2 constitution: re-verify or trust.
- Switch propagation: propagate or not.
- Claim provenance: per-field or flat.

8 configurations. Probe corpus runs against each.

**Output.** A matrix of attack success rates per (probe class, configuration).
Stored as CSV under
`agent-service/evals/multi_hop_injection_study/results/<run-id>.csv`.

**Eval harness.** Existing `agent-service/evals/` infrastructure. Add a
`multi_hop_injection_study/` suite. Each probe is a fixture; each
configuration is an axis; results are a matrix.

**Reproducibility.** Fixed seed, fixed model snapshot, deterministic rule
engine, recorded primitive responses (no live RPC during eval). Same probe
plus same configuration must yield the same outcome bit for bit.

## Threats to validity

- **Hand-authored probes miss real attack patterns.** Mitigation: red-team
  session at end of study; attempt to find probes that bypass the defenses
  picked. Failures inform a v2 corpus.
- **Single model snapshot.** Results may not generalize across models.
  Mitigation: run the headline configuration against 2 to 3 model families if
  free-tier OpenRouter quota allows.
- **Specialist agents too narrow.** If both prompts are tightly scoped, the
  LLM has little surface to be manipulated on. This is the Q4 null-result
  risk. Mitigation: probe corpus deliberately targets the narrow surface; if
  even this fails to elicit attacks, the null is itself the finding.
- **Token metadata content in the wild is mostly mainstream-token names and
  marketing copy.** Today's attack prevalence on Solana metadata fields is
  low. The study's threat model assumes metadata content can be
  attacker-controlled (it can, trivially: any token issuer chooses these
  fields and the off-chain JSON the uri points to has no validation at all);
  the study is forward-looking on the threat surface, not retrospective on
  observed incidents.
- **Two agents in one codebase share defenses.** The study cannot
  generalize to cross-organization A2A where defense stacks differ. That
  scope is out of scope; declared up front.

## Out of scope

- Defenses against attacks at boundary 1 only. Well-studied elsewhere.
- Cross-organization A2A protocols. Different threat model, different
  infrastructure.
- Agent-orchestration patterns (planner, executor, synthesizer ensembles).
  This study has exactly two agents and one direction of flow. The handoff is
  envelope-shaped, not protocol-shaped.
- Article runner (#37). Adds a boundary 1' (article text instead of token
  metadata) but does not change the boundary-2 analysis. Article runner is a
  separate study reusing the same boundary-2 framework.

## Done when

- Probe corpus authored, 30 to 50 cases across the 3 classes, committed under
  `agent-service/evals/multi_hop_injection_study/probes/`.
- Eval harness runs the corpus against all 8 defense configurations
  deterministically.
- Results matrix published in this doc with discussion of which
  configurations hold against which probe classes.
- One short writeup summarizing findings. Suitable for
  `docs/engineering-blogs/` if results are interesting; suitable for archival
  in `docs/references/` if null.
- Codebase carries the defenses chosen by the winning configuration. Losing
  configurations are removed entirely. No parallel paths in production code
  per AGENTS.md rule.

## Build sequence

Each step is one PR-sized change. The first three deliver product utility
alongside the research goal; steps 4 and 5 are the research artifact itself.

1. **Rule engine.** Rust-side, on `solana.raw-edges`. 4 to 5 deterministic
   rules emit `Candidate` envelopes onto `solana.candidates.v1`. No agent
   yet. Eval can fixture-replay these later. ClickHouse table for candidate
   archival follows the existing edge pattern.
2. **Monitor agent specialist.** Own system prompt, own tool surface (token
   metadata enrichment via `get_token_info`, structural context, claim
   emit). Runs through `run_one_turn` with `task="triage"` envelope. Driver
   consumes `solana.candidates.v1` and emits alerts (where alerts go is a
   product question; for the study, alerts land in a topic the eval harness
   reads). No connection to Q&A yet.
3. **Q&A handoff.** Extend `TurnEnvelope` with `prior_claims: tuple[Claim, ...]`.
   Chat driver, when the user opens a thread seeded with a monitor alert,
   builds the envelope with `prior_claims` pre-populated. Q&A's primary
   agent system prompt is updated to treat `prior_claims` as evidence not
   ground truth.
4. **Probe corpus plus harness.** Authored corpus, deterministic eval, 8
   configurations, results matrix.
5. **Run, analyze, writeup.** Final defenses kept; losing configurations
   removed.

## What gets cleaned up after the study

The 2x2x2 configuration grid is research scaffolding, not production
architecture. After the study lands, the codebase carries exactly one
defense configuration at boundary 2, the one the data picks. Switch
matrices, configuration toggles, and per-config code paths are removed
entirely. No parallel paths.

The probe corpus stays as a regression suite under `agent-service/evals/`.
Future changes to defenses run against the corpus before merging.
