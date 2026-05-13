#
# Project-wide task runner. Codegen entry point for everything that
# crosses a service boundary.
#

# Auto-load `.env` so recipes that need credentials (e.g. `just eval`
# reading CLICKHOUSE_PASSWORD) work without a `set -a; source .env`
# dance. .env is gitignored; .env.example is committed.
set dotenv-load := true

default:
    @just --list

# Run the full agent-service pytest suite. Wiring-only; no LLM calls.
# Per phase A.5 the baseline budget is <5s; longer than that means a
# real OpenRouter call snuck past the TestModel boundary.
test:
    cd agent-service && uv run pytest -v

test-unit:
    cd agent-service && uv run pytest tests/unit -v

test-integration:
    cd agent-service && uv run pytest tests/integration -v

# Regenerate every wire-type artifact from the proto source of truth.
#
# Single source: `proto/multichain/wire/{shared,agent}/v1/*.proto`.
# Three generators (all maintained per AGENTS.md library bar):
#   - Rust:   buffa (Anthropic, pure Rust + JSON + zero-copy views)
#   - Python: protobuf (Google official)
#   - TS:     @bufbuild/protoc-gen-es (Buf, ESM-native)
#
# Output (all checked in):
#   backend/src/wire/generated/                    (Rust mod tree)
#   agent-service/src/multichain/                  (Python top-level pkg)
#   frontend/src/lib/wire/                         (TS pkg tree)
#
# Wire format: proto canonical JSON encoding (camelCase fields,
# oneof-wrapped discriminator unions). See AGENTS.md "Idiomatic-first".
regen-wire-types:
    @echo ">> linting protos"
    buf lint
    @echo ">> generating Rust + Python + TS from protos"
    rm -rf backend/src/wire/generated agent-service/src/multichain frontend/src/lib/wire
    mkdir -p backend/src/wire/generated agent-service/src/multichain frontend/src/lib/wire
    buf generate
    @echo ">> wire types regenerated"

# Run an eval suite against the running agent service. Each case
# POSTs to /agent/ask with runType=eval (so the resulting traces
# carry mcae.run.type=eval and stay filterable in CH/Langfuse),
# captures the trace id from the AgentDone SSE frame, then runs
# every probe in the case against otel.otel_traces by trace id.
# Per-probe ProbeResult JSON + a RunMetadata summary land under
# evals/runs/<run_id>/. Exits non-zero if any probe failed.
eval suite="evals/cases/wallet_profile_smoke.yaml":
    uv --directory agent-service run python -m agent_service.evals \
        "{{ absolute_path(suite) }}" \
        --runs-root "{{ justfile_directory() }}/evals/runs" \
        --baselines-root "{{ justfile_directory() }}/evals/baselines"

# Refresh a suite's committed regression baseline from the latest
# matching run. Run `just eval <suite>` first; this consumes the
# run artifacts and writes evals/baselines/<suite>.json. Refuses
# to lock in failing probes without --force; the escape hatch is
# for philosophy-2 cases where a known-failing probe IS the
# contract.
eval-baseline suite *flags:
    uv --directory agent-service run python -m agent_service.evals.update_baseline \
        "{{ absolute_path(suite) }}" \
        --runs-root "{{ justfile_directory() }}/evals/runs" \
        --baselines-root "{{ justfile_directory() }}/evals/baselines" \
        {{ flags }}

# Print wallets currently observable in the live window. The
# rolling 60s window means any pinned wallet in a `wallet_profile`-
# shaped eval suite eventually ages out. When a suite starts
# failing with "wallet not in current live window", run this to
# pick a fresh address, paste it into the case yaml, then
# `just eval` + `just eval-baseline` to re-mint. Args mirror the
# script's: `--window 60 --limit 5 --addr-only`.
eval-pick-wallet *flags:
    uv --directory agent-service run python -m agent_service.evals.pick_observable_wallet {{ flags }}

# Tear the whole compose stack down (including volumes!) and rebuild
# from a clean slate. Use when env-driven config has shifted (model
# ids, provider defaults, ClickHouse schema) and you want zero
# residue from the previous run. `--volumes` wipes ClickHouse +
# redpanda + langfuse-postgres state too: ingestion will replay
# from genesis and Langfuse will re-bootstrap the org/project on
# next boot, so don't run this when there's data you care about.
# `--build -d` forces a fresh image build, then detaches.
#
# Tears down + rebuilds the full docker compose stack from scratch.
docker:
    docker compose down --volumes
    docker compose up --build -d

# Why force the port: a stale `next dev` from a crashed terminal
# silently bumps a fresh start to 3009, which breaks any hardcoded
# NEXT_PUBLIC_AGENT_URL or agent-service CORS allowlist pointing at
# :3008. Killing first keeps every dev session pinned to the same
# URL.
#
# `lsof -ti :3008` lists PIDs holding the port. The leading `-`
# tells just to ignore the recipe-line exit code, so an empty PID
# list (no process to kill, lsof exits non-zero on macOS) does not
# abort the recipe; the redirect silences the cosmetic error.
#
# Frees port 3008 then runs the Next.js frontend dev server on it.
dev:
    @echo ">> freeing port 3008 if in use"
    -lsof -ti :3008 | xargs kill -9 2>/dev/null || true
    @echo ">> starting frontend on :3008"
    cd frontend && pnpm dev --port 3008
