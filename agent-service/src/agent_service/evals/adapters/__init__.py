"""Eval framework adapters. Each module here implements one
`framework_adapter` value from `schema.FrameworkAdapter`. The runner
selects an adapter by name and calls its `run_case` function.

The `framework_free` (a.k.a. `_stub`) adapter dispatches to our pure-
function probes directly with no framework involvement. The
`pydantic_evals` adapter (next change) wraps the same probes as
pydantic_evals Evaluators so we get its case dataset / scorer / report
machinery for free, while keeping probe semantics in our code.
"""
