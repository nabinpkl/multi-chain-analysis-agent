# Security policy

## Reporting a vulnerability

Email **contact@nabin.org** with the subject line `SECURITY: multi-chain-analysis-agent`. Please do not open a public GitHub issue for vulnerability reports.

Best-effort response within 7 days. This is a solo-maintained open-source project, not a funded program, so expect best-effort timelines rather than SLAs.

If you would prefer encrypted reporting, mention it in the first email and a PGP fingerprint can be exchanged.

## In scope

This project is fundamentally about LLM-agent defense, so the defense surface is explicitly in scope.

- **Prompt injection** against the agent via:
  - On-chain token metadata fields (`name`, `symbol`, `uri`).
  - Memo program payloads.
  - User chat input.
  - Any other attacker-controlled byte stream that reaches the model context.
- **Output-gate bypasses.** Anything that lets an unverified claim, a fabricated number, or an ungrounded narrative reach the SSE wire. Cases of interest:
  - Bypassing the placeholder gate.
  - Bypassing the structural value-compare against the binding store.
  - Convincing the LLM judge to pass an ungrounded claim.
- **Meta-defense attacks.** Attacks that target the LLM judge itself (the judge is downstream of the agent and receives attacker-influenced text). See [docs/securing-agents/07-meta-defense-trust-boundary.md](docs/securing-agents/07-meta-defense-trust-boundary.md).
- **Runtime-parity violations.** A defense that works under one runtime (pydantic-ai or codex) but not the other.
- **Switch-surface integrity.** Anything that lets a request silently change defense state without it appearing in the trace, or that breaks the contract that every switch maps to at least one eval case.
- **Resource-bound escapes.** Anything that lets an anonymous principal exceed the declared per-turn budgets (tokens, db_time_ms, tool_calls, sessions).
- **Type confusion** in any wire boundary defined in `proto/multichain/wire/{shared,agent}/v1/`.

A working proof-of-concept eval case (hermetic, under `evals/cases-hermetic/`) is the gold-standard report format. If the issue can be reproduced as a probe regression against a committed baseline, the fix and the regression test land together.

## Out of scope

These are explicitly out of scope. Reports about them will be closed without action.

- **Any public demo instance.** Hosted demos of this codebase are best-effort with no SLA. Please test against your own deploy, not someone else's. DDoS, surface enumeration, and port scanning against a public instance are not interesting and not authorized.
- **Authentication, multi-tenancy, RBAC.** None of these exist by design (see [PRD.md](PRD.md)). "There is no auth" is not a vulnerability.
- **Anonymous-visitor switch toggling.** Switches are reachable from any client on purpose so a visitor can flip a defense off and watch what regresses. This is the intended demo behavior.
- **Generic dependency-CVE noise** (Dependabot-style "version X.Y.Z has CVE-NNNN"). Open a regular PR or issue with a clear exploitation path against this project specifically.
- **Self-XSS, missing security headers on the demo VM, clickjacking on a project with no auth.** Same reason as the demo-VM bullet above.
- **Findings against third-party services this project depends on** (Langfuse, ClickHouse, Redpanda, Solana RPC providers). Please report those upstream.

## Disclosure

Coordinated disclosure preferred. Once a fix lands, the vulnerability and the test case that pins it will be described in the relevant commit message and, where appropriate, in the security chapter under [docs/securing-agents/](docs/securing-agents/).
