#
# Project-wide task runner. Phase A of Python-agent migration introduced
# this file as the codegen entry point. Recipes here are the canonical
# way to run anything that crosses the Rust ↔ Python boundary.
#

default:
    @just --list

# Regenerate every wire-type artifact in the right order.
#
# Today (Phase A) this runs the Rust → Python pydantic flow. Phase B.2
# extends it with the Python → Frontend TS flow (json-schema-to-typescript).
# The Rust → Frontend TS flow (ts-rs) runs as part of `cargo test --bin
# server` and isn't invoked here.
#
# Output:
#   agent-service/src/agent_service/wire/schemas-shared/*.json (checked in)
#   agent-service/src/agent_service/wire/shared.py             (checked in)
#
# Pre-commit hook (future) runs this and fails if anything is dirty.
regen-wire-types: regen-shared-types

regen-shared-types:
    @echo ">> dumping JSON schemas from rust"
    cd backend && cargo run --quiet --bin dump_schemas
    @echo ">> generating pydantic models from schemas"
    rm -rf agent-service/src/agent_service/wire/shared
    cd agent-service && uv run datamodel-codegen \
        --input src/agent_service/wire/schemas-shared/ \
        --input-file-type jsonschema \
        --output src/agent_service/wire/shared/ \
        --output-model-type pydantic_v2.BaseModel \
        --use-schema-description \
        --use-standard-collections \
        --use-union-operator \
        --target-python-version 3.12 \
        --use-double-quotes
    @echo ">> writing wire/shared/__init__.py re-export shim"
    cd agent-service && uv run python scripts/build_shared_init.py
    @echo ">> wire types regenerated"
