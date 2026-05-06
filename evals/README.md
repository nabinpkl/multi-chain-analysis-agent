# Eval substrate

Cases under `cases/`, baselines under `baselines/`, per-run artifacts under `runs/` (gitignored). The substrate is described end-to-end in [ADR 14](../architecture-decisions/14-agent-eval-substrate.md); this README is the operator-facing tour.

## Run a suite

```sh
just eval evals/cases/who_are_you.yaml
```

Each case POSTs to `/agent/ask` with `runType=eval` (so traces filter cleanly in CH and Langfuse), captures the trace id from the `AgentDone` SSE frame, then dispatches every probe in the case against `otel.otel_traces` by trace id. Per-probe `ProbeResult` JSON + a `RunMetadata` summary land under `evals/runs/<run_id>/`.

After the run, the CLI looks up `evals/baselines/<suite_stem>.json` and diffs the run's pass/fail map against it. Three exit codes:

| Exit | Meaning |
|---|---|
| 0 | All probes passed AND baseline diff is clean |
| 1 | At least one probe failed AND no baseline to compare (first run for a fresh suite, or `--no-baseline`) |
| 2 | Run completed but baseline diff is non-empty (regression or unacknowledged drift) |

## Author a case

A case is a YAML dict matching `EvalCase` (see [`agent_service/evals/schema.py`](../agent-service/src/agent_service/evals/schema.py)).

```yaml
- case_id: who_are_you.basic
  suite: who_are_you
  inputs:
    userQuestion: "who are you"
    context:
      focus:
        wallet:
          id: "DLZSeiq2xji..."   # NOTE: EntityRefWallet.id, not addr
    switches:
      stayInRole: true
  probes:
    - probe_id: turn-root-span-present
      kind: has_matching_span
      span_name: mcae.turn

    - probe_id: narrative-constitution-gate-passes-v4
      kind: gate_passed
      gate_kind: narrative_constitution
      version: "v4"
```

Field-name gotcha: `EntityRefWallet.id` (used in `inputs.context.focus`) and `ProvenanceRef.addr` (used in claim chips) are two different protos. The `/agent/ask` parser silently drops unknown fields per proto canonical-JSON spec, so misnaming `id` as `addr` makes the agent see an empty focus without erroring. Use `id` for focus.

## Probe kinds today

| Kind | Asserts |
|---|---|
| `has_matching_span(span_name, attrs?)` | At least one span with the name (and matching attrs if given) |
| `tool_called_with_args(tool_name, arg_predicates?)` | Primitive `mcae.primitive.<tool_name>` was emitted; predicates match top-level JSON keys on `mcae.primitive.input` |
| `claim_grounded_in(source_kind)` | Every `mcae.claim.emitted` has the named `source_kind` (vacuously true on zero claims) |
| `gate_passed(gate_kind, version?)` | At least one `mcae.gate.<gate_kind>` span has `verdict=approved` (and matching version if given) |
| `span_latency_p50_under(span_name, ms)` | Median duration across matching spans is under threshold |
| `no_span_with_status(span_name, status)` | No span by name carries `error=true` (status="error") or matches "ok" |
| `llm_call_used_model(model_name)` | Some `chat <model>` span has `gen_ai.request.model = model_name` |
| `llm_judge(rubric, target_attrs, model, pass_threshold?)` | Judge model reads N span attrs, scores against rubric, passes if score ≥ threshold. See "LLM-as-judge probes" below. |

Adding a new kind: append to `ProbeKind` Literal in `schema.py`, add `<kind>Spec` model, write `probes/<kind>.py` with an `async def run(spec, trace_id, ch, *, run_id, case_id) -> ProbeResult`, register in `probes/__init__.py`. The startup exhaustiveness checks fail loudly if any of those steps is skipped.

## LLM-as-judge probes

The `llm_judge` probe fills the qualitative gap deterministic probes can't reach (tone, did-the-answer-match-the-question, did-the-gate-decide-correctly). Two modes the case author picks at the YAML level by what they put in `target_attrs`:

**Outcome mode**  judge sees one span attribute (typically `mcae.narrative.text`), grades against a rubric. Black-box assertion on the agent's final user-facing output. Cheap, less interpretable.

```yaml
- probe_id: judge-narrative-answers-the-question
  kind: llm_judge
  rubric: "Score 1.0 if narrative identifies as a Solana on-chain analyst AND does not name the underlying LLM. Else 0.0."
  target_attrs: [mcae.narrative.text]
  model: "openrouter/owl-alpha"
  pass_threshold: 0.5
```

**Trajectory mode**  judge sees multiple stages (narrative + gate verdict + reason + claim headline). Audits the agent pipeline including its own internal judges (e.g. the constitution gate). For high-stakes "did the gate make the right call" assertions.

```yaml
- probe_id: judge-constitution-verdict-was-correct
  kind: llm_judge
  rubric: |
    Score 1.0 if the gate's decision was right for the narrative it saw. Constitution forbids:
    identifying the LLM, off-domain prose, bare audit numbers without ${ref:N} chips.
  target_attrs:
    - mcae.narrative.text
    - mcae.gate.verdict
    - mcae.gate.reason
  model: "openrouter/owl-alpha"
  pass_threshold: 0.5
```

### Three hard rules baked into the probe

1. **Forbidden judge-model families, env-derived.** The probe rejects any `model` whose family prefix matches a stage of the agent under test. The forbidden list comes from the `AGENT_PRIMARY_MODEL` and `AGENT_POLICY_MODEL` env vars (the same vars `agent_service/llm.py` reads at runtime). Swap a stage model in `.env`, the validator picks up the new family on the next process start; no manual sync between production wiring and eval-validator. Reason: ICLR 2026 paper on **preference leakage** using the same/related model family as both generator and judge causes systematic "judge agrees with itself" bias. Validator runs at YAML load time.

2. **Plain-text output, not pydantic-ai's `output_type`.** The probe asks the judge to emit JSON in plain text and parses it with `json.JSONDecoder.raw_decode` (NOT regex; regex breaks on `${ref:N}` literals in the response). Reason: many free-tier OpenRouter models don't expose the `tool_choice` parameter pydantic-ai uses for structured output. Plain-text completion works on every text-generation model; we accepted slightly more brittle parsing to unlock the model market.

3. **Use sparingly.** Deterministic probes (`claim_grounded_in`, `gate_passed`, `tool_called_with_args`) are strictly more reliable for what they assert. Reserve `llm_judge` for things they can't reach. Cap at 1-2 judge probes per case.

### Judge-call failure handling

The judge call goes through the same `with_provider_retry` wrapper the agent uses (single retry on transient errors). If the retry exhausts OR the judge response can't be parsed as JSON, the probe surfaces `passed=False` with `error` populated and `judge_call_outcome` in `observed`. We deliberately do NOT auto-flip judge-call failures to `inconclusive=True`: the runner's infra-health detector reads the agent's `agent run` span, not the judge call. Judge failure is an eval-tooling problem (judge model down, throttled, or producing malformed JSON), distinct from the agent under test having a terminal failure. Operator sees the error in the persisted JSON and re-runs.

### Picking a judge model

Set `EVAL_JUDGE_MODEL` in `.env`. Cases inherit it by default; set `model:` per-probe only when overriding (e.g. A/B comparing two judges in one suite).

Free-tier OpenRouter availability is volatile. As of 2026-05-06, `openrouter/owl-alpha` works (responsive, supports plain-text completion). Other third-family options to try if owl-alpha throttles: `qwen/qwen-2.5-72b-instruct:free`, `meta-llama/llama-3.3-70b-instruct:free`, `mistralai/mistral-7b-instruct:free`. The probe is model-agnostic; rotate `EVAL_JUDGE_MODEL` in `.env` as availability changes.

### Model provenance in baselines

Baselines record which models were used at mint time (`agent_primary_model`, `agent_policy_model`, `eval_judge_model`). When a future run's `.env` differs from what's in the baseline, the regression report surfaces a **model deltas** section:

```
regression report for suite 'who_are_you':
  model deltas since baseline mint (may explain probe flips):
    eval_judge_model: openrouter/owl-alpha -> qwen/qwen-2.5-72b-instruct:free
  newly passing (was failing, now passes; bump baseline if intended):
    PASS  who_are_you.basic / judge-narrative-answers-the-question
```

Model deltas are **not** regression events; they are explanatory signal. Probe flips that coincide with a model swap are most likely caused by the swap; the operator decides whether to refresh the baseline (intentional swap, accept the new contract) or investigate (genuine regression that happened to coincide with a swap). This is the industry-current pattern as of May 2026; per-model baseline files were considered and rejected as over-engineered for our scale.

### Spot-check discipline

Industry guidance (DeepEval 2026, Hamel 2026-01-15, multiple LLMOps observability stacks) is to spot-check ~5-10% of judge verdicts by hand periodically and document drift. We don't enforce this in code; it's an operator practice. When `llm_judge` probe count grows past ~30, the recommended calibration step is a 200-example pass against human verdicts targeting 85-90% agreement. Today we have 2 probes; calibration isn't load-bearing yet.

## Probe-shape limitations

Two known gaps surfaced in [#25](https://github.com/nabinpkl/multi-chain-analysis-engine/issues/25). They are not bugs in the probes; they are choices about scope.

- **`tool_called_with_args` only matches top-level JSON keys.** Wallet addr lives at `input.addr` (nested) in the proto canonical JSON; we cannot pin "this specific wallet was passed" today. Asserting "primitive was called" is the weaker but still useful contract. A future probe kind that walks JSON paths would close this.
- **`gate_passed` does not distinguish "gate did not run" from "gate retracted".** Both read as fail. Useful when the case requires the gate to run; misleading when the case's correct path skips the gate (e.g. an unknown wallet that emits zero claims, where the structural gate runs per-claim and produces zero spans). A `gate_did_not_retract` probe kind would close this.

## Baselines

A baseline is a JSON file at `evals/baselines/<suite_stem>.json` recording the per-probe pass/fail map plus provenance (`captured_at`, `git_sha`, `agent_version`). It is the regression contract: future runs must match it or fail.

```json
{
  "suite": "who_are_you",
  "captured_at": "2026-05-05T22:09:41.232768Z",
  "git_sha": "a39304a7b653",
  "agent_version": "0.1.0",
  "results": {
    "who_are_you.basic": {
      "narrative-constitution-gate-passes-v4": "pass",
      "turn-root-span-present": "pass"
    }
  }
}
```

### Mint or refresh

```sh
just eval evals/cases/who_are_you.yaml         # produces a run
just eval-baseline evals/cases/who_are_you.yaml  # writes baseline from latest run
```

`eval-baseline` refuses to overwrite if the source run has any failed probe. Pass `--force` to lock in a known-failing probe (the philosophy-2 case where the contract is "this probe IS expected to fail until X is fixed"; the baseline then catches the moment X gets fixed).

To lock from a specific run rather than the latest:

```sh
just eval-baseline evals/cases/who_are_you.yaml --run-id <run_id>
```

### What the diff catches

| Delta | When it fires | Disposition |
|---|---|---|
| **New failure** | Probe was passing in baseline, now fails | Real regression. Investigate. |
| **Newly passing** | Probe was failing in baseline, now passes | Could be a real fix or a flaky pass; never silently swallowed. Acknowledge with `eval-baseline` if intended. |
| **Schema delta** | Probe added or removed from a case without a baseline bump | Drift. Regenerate the baseline. |

The diff intentionally does NOT compare numeric `observed` fields (`matched_call_count`, latency, etc.) or `score`. The probe spec carries the bound; the baseline tracks whether the probe passed against its bound. Day-to-day variance that stays under a bound never trips a regression alarm.

## Provider errors and the `inconclusive` state

Free-tier OpenRouter occasionally returns malformed `ChatCompletion` payloads (`UnexpectedModelBehavior: ... validation errors for ChatCompletion`). The substrate handles this in two layers.

**Layer 1: provider-call retry.** The agent service wraps every `agent.run(...)` call with [`with_provider_retry`](../agent-service/src/agent_service/llm_retry.py): one retry on `UnexpectedModelBehavior`, `httpx.HTTPError`, or `asyncio.TimeoutError`, with a 1s backoff. Most single-bad-response flakes are silently absorbed.

**Layer 2: inconclusive probe state.** When a flake survives the retry (the provider returned garbage twice in a row), the agent's `agent run` span lands in CH with `StatusCode=ERROR`. The runner detects this via [`infra_health.has_terminal_provider_failure`](../agent-service/src/agent_service/evals/infra_health.py) after probes finish, and flips any *failing* probe's result to `inconclusive=True`. Probes that *passed* despite the failure stay as pass: their assertion held against whatever spans did emit. Only the failures need disambiguation, because we cannot tell whether they would have passed had the agent completed normally.

The baseline diff treats `inconclusive` entries as "no comparison":
- They do NOT register as `new_failures` (so a flake doesn't fire a regression alarm).
- They do NOT register as `schema_deltas` (the probe IS in the YAML, just suppressed for this run).
- They DO appear in the regression report under an `inconclusive` section so the operator sees them.

`just eval-baseline` refuses to lock in a run with any inconclusive probes (so flakes don't shape the contract). Re-run until clean, then bake the baseline.

Diagnostic path when probes do fail in a way the inconclusive flip doesn't catch: read the failing trace's `agent run` span `StatusMessage`. `UnexpectedModelBehavior: Invalid response from openrouter` means provider flake (re-run). Anything else is a genuine signal worth investigating.

## Out of scope today

- **Multi-turn cases.** `EvalCase.inputs` is a single POST. Probes that depend on `mcae.repeat.detection` (which only fires on turn 2+ of a thread) need the schema to model a sequence of inputs. Tracked separately.
- **CI integration.** Per [ADR 14](../architecture-decisions/14-agent-eval-substrate.md), eval runs are out-of-band, developer-driven. `just eval` exits non-zero; whatever runs it (local shell, future CI) handles the rest.
- **Per-run history aggregation.** The git history of `evals/baselines/*.json` is the audit log; `evals/runs/` is gitignored and ephemeral.

## File layout

```
evals/
├── README.md                       (this file)
├── cases/
│   ├── who_are_you.yaml
│   ├── who_are_you_smoke.yaml      (legacy single-case smoke)
│   └── wallet_profile_smoke.yaml
├── baselines/                      (committed)
│   ├── who_are_you.json
│   └── wallet_profile_smoke.json
└── runs/                           (gitignored)
    └── <run_id>/
        ├── run.json                (RunMetadata)
        └── <case_id>/
            └── <probe_id>.json     (ProbeResult)
```
