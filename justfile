#
# Project-wide task runner. Codegen entry point for everything that
# crosses a service boundary.
#

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

