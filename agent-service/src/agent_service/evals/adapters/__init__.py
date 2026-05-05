"""Eval framework adapters. Each module here implements one
`framework_adapter` value from `schema.FrameworkAdapter`. The runner
selects an adapter by name and calls its `run_case` function.

Only the `framework_free` (`_stub`) adapter is wired today. It
dispatches to our pure-function probes directly with no framework
involvement. ADR 14's 2026-05-05 addendum explains why a
pydantic_evals adapter, originally planned as Layer 4, was dropped:
its span-querying primitive captures spans in-process, which is
incompatible with our cross-process OTel → ClickHouse pipeline.
The seam stays here as a single-arm dispatch so a future adapter
that does fit our architecture can slot in without reshaping the
runner.
"""
