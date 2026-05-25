# Contributing

Thanks for the interest. This is a solo-maintained open-source agent-design exercise; contributions are welcome and reviewed best-effort.

## Read this first

[AGENTS.md](AGENTS.md) is the authoritative ruleset. It covers:

- Dependencies (maintenance bar, why LiteLLM is banned, etc.).
- Wire types (single proto source of truth, no hand-typed cross-service types).
- Idiomatic conventions per language.
- The "no dead code, no backward-compat layers, no parallel paths" rule.
- Commit-message convention.
- Known limitations.

A PR that violates an AGENTS.md rule without a recorded justification will be sent back. If a rule itself looks wrong for the change you are making, propose the AGENTS.md edit in the same PR rather than working around the rule.

## Setup

Prereqs and run commands are in [README.md](README.md). Full env-var inventory and the local dev contract are in [SPEC.md](SPEC.md#local-dev-contract).

```bash
cp .env.example .env
$EDITOR .env                          # fill required secrets
docker compose --profile eval up -d --build
just test                             # agent-service wiring tests, <5s
```

## Pre-PR checklist

Run the relevant subset of these before pushing:

```bash
just test                             # always
just regen-wire-types                 # if you touched anything in proto/
just eval-hermetic <your-suite>       # if you touched the agent loop, the
                                      # output gate, or any defense surface
cargo test --manifest-path backend/Cargo.toml   # if you touched Rust
pnpm --dir frontend lint              # if you touched the frontend
```

After any backend feature change, per AGENTS.md, run `docker compose up -d --build` at the end of the change to catch image-build regressions early.

## Commit messages

Commit messages describe what the change does and why. Do NOT reference internal narrative scaffolding (ship N, pass M, session K). Reference a tracked issue (`#NNN`) when one exists; otherwise just describe the change.

- Subject: an action phrase ("add X", "fix Y", "refactor Z"). Under 70 chars.
- Body: what the change does and the reasoning behind it. A reader six months from now should understand the commit from its message alone.

## When to add an ADR

Add a record under [architecture-decisions/](architecture-decisions/) when the change makes a load-bearing technical choice that future readers will need context for: a new runtime, a new wire format, a new defense layer, a database engine swap, anything that changes a contract in [SPEC.md](SPEC.md).

Do NOT add an ADR for:

- Bug fixes.
- Refactors that preserve behavior.
- Dependency bumps.
- Documentation updates.
- Renames.

When in doubt, ask in the PR.

## Scope of contributions

Welcome:

- Eval cases (especially hermetic injection / output-gate cases under `evals/cases-hermetic/`).
- Bug fixes with a reproducing test.
- Defense improvements with both a positive and a negative eval case.
- Documentation fixes.
- Performance improvements with measurements.

Please open an issue to discuss first:

- New primitives.
- New runtimes (a third one alongside pydantic-ai and codex).
- Changes to the wire format.
- Anything that touches the canonical-mint registry.
- Anything that adds a new dependency. The maintenance bar in [AGENTS.md](AGENTS.md) applies.

Out of scope, per [PRD.md](PRD.md):

- Chains other than Solana.
- Auth, multi-tenancy, accounts.
- Trading signals, MEV analysis, financial-advice surfaces.

## Reporting security issues

See [SECURITY.md](SECURITY.md). Do not file security issues as public GitHub issues.

## License

By contributing, you agree your contributions are licensed under the [Apache License 2.0](LICENSE), the same license that covers this project.
