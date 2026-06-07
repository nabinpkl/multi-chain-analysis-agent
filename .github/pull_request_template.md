<!--
Keep the description readable six months out. No personal-plan vocabulary
("Ship N", "Pass M", "Session K") in the title, commits, or body. Subject
line is an action phrase: "add X", "fix Y", "refactor Z".
-->

## What this changes

<!-- What changed and why, in prose. Reference #NNN if an issue exists. -->

## Why

<!-- The problem being solved, or the decision being recorded. -->

## Checklist

<!-- Tick what applies; delete what doesn't. These mirror AGENTS.md. -->

- [ ] The new path AND its cutover ship together (no parallel ways to reach
      the same observable behavior; no dead code left behind).
- [ ] No god files/functions introduced; mid-work mess found was flagged or
      cleaned, not silently worked around.
- [ ] Python: absolute imports only (`from agent_service...`); type hints
      present; no relative imports.
- [ ] No hardcoded URLs / rate limits / configs that belong in `.env`.
- [ ] Cited any new doc/library claim with a verified publication date
      (web search ranks for relevance, not recency).

### If this touches `proto/`
- [ ] Ran `just regen-wire-types` and committed the regenerated output
      (the `wire-drift` CI job fails otherwise).

### If this adds a dependency
- [ ] Passes the AGENTS.md library bar (released within ~1 month, ≥100
      stars, real human maintenance, no deprecation notice), or the
      exception is recorded in `docs/dependency-exceptions.md`.

## Verification

<!--
What you actually ran. Report faithfully: if a check was skipped or a
test failed, say so. For a backend feature change, AGENTS.md asks for
`docker compose up -d --build` at the end.
-->

- [ ] `cd backend && cargo test`
- [ ] `cd frontend && pnpm exec tsc --noEmit`
- [ ] `cd agent-service && uv run pytest` (if the agent plane changed)
- [ ] `docker compose up -d --build` (backend feature changes)
