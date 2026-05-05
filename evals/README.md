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

Adding a new kind: append to `ProbeKind` Literal in `schema.py`, add `<kind>Spec` model, write `probes/<kind>.py` with an `async def run(spec, trace_id, ch, *, run_id, case_id) -> ProbeResult`, register in `probes/__init__.py`. The startup exhaustiveness checks fail loudly if any of those steps is skipped.

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

## Runtime flakes

Free-tier OpenRouter occasionally returns malformed `ChatCompletion` payloads (`UnexpectedModelBehavior: ... validation errors for ChatCompletion`). A run that hits this fails ~3 probes in `wallet_profile.basic` (no narrative, no claim, no structural gate). Diagnose by reading the trace's `agent run` span's `StatusMessage`; if the cause is provider flake, re-run. If it's deterministic across N runs, treat as a real regression.

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
