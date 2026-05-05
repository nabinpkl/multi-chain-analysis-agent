"""Agent eval substrate (Ship 2 of agent-observability, ADR 14).

Four-layer stack:

    Layer 4: framework adapter (`adapters/`)        thin, swappable
    Layer 3: runner (`runner.py`)                   framework-agnostic
    Layer 2: probes (`probes/`)                     pure functions, ours
    Layer 1: schema (`schema.py`)                   canonical types, ours

This package's public surface is the `schema` module. Probes, runner,
and adapter consume the schema; the schema consumes nothing from
this package or any framework.
"""
