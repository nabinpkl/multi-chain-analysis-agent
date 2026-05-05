"""Tests for the ClickHouseClient wrapper.

Two concerns:

1. The wrapper preserves the safe path through clickhouse-connect:
   the SQL string is sent unchanged with `{name:Type}` placeholders,
   and dynamic values reach the underlying client via the
   `parameters=` kwarg (which the library forwards as `param_<name>`
   URL args, server-side bound). This test catches the load-bearing
   regression: a future "helpful" refactor that does
   `sql.format(**params)` somewhere in the wrapper would silently
   reintroduce client-side substitution.

2. Result rows are returned as dicts keyed by column name (not
   the library's tuple-of-rows + separate column-names shape).

We mock the underlying AsyncClient because clickhouse-connect's
own behavior is well-tested upstream; we're only asserting that
our wrapper calls into it correctly.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_service.evals.ch import ClickHouseClient


@pytest.mark.asyncio
async def test_query_passes_sql_and_params_through_unmodified() -> None:
    """The SQL body must reach the underlying client byte-for-byte;
    values must arrive in the `parameters=` kwarg, not interpolated."""
    upstream = AsyncMock()
    upstream.query = AsyncMock(
        return_value=SimpleNamespace(
            column_names=["n"],
            result_rows=[(1,)],
        )
    )
    wrapper = ClickHouseClient(upstream)

    sql = "SELECT count() AS n FROM t WHERE id = {id:String}"
    rows = await wrapper.query(sql, id="x'; DROP TABLE t; --")

    upstream.query.assert_awaited_once()
    call_args = upstream.query.await_args
    # The first positional arg is the SQL, untouched.
    assert call_args.args[0] == sql
    assert "DROP TABLE" not in call_args.args[0]
    # Values reach the library via `parameters=`, not interpolated.
    assert call_args.kwargs == {"parameters": {"id": "x'; DROP TABLE t; --"}}
    # Result is rebuilt as list[dict] keyed by column name.
    assert rows == [{"n": 1}]


@pytest.mark.asyncio
async def test_query_zips_columns_and_rows_into_dicts() -> None:
    """Multiple columns + multiple rows zip cleanly."""
    upstream = AsyncMock()
    upstream.query = AsyncMock(
        return_value=SimpleNamespace(
            column_names=["span_name", "n"],
            result_rows=[
                ("mcae.gate.placeholder", 3),
                ("mcae.gate.structural", 1),
            ],
        )
    )
    wrapper = ClickHouseClient(upstream)
    rows = await wrapper.query("...")
    assert rows == [
        {"span_name": "mcae.gate.placeholder", "n": 3},
        {"span_name": "mcae.gate.structural", "n": 1},
    ]


@pytest.mark.asyncio
async def test_aclose_forwards_to_underlying() -> None:
    upstream = AsyncMock()
    upstream.close = AsyncMock()
    wrapper = ClickHouseClient(upstream)
    await wrapper.aclose()
    upstream.close.assert_awaited_once()


def test_query_signature_is_positional_only_for_sql() -> None:
    """The `/` after `sql` means a caller cannot pass
    `query(sql=f"...{x}...")`  the keyword form is rejected at
    call time, eliminating that one shape of accidental
    interpolation."""
    import inspect

    sig = inspect.signature(ClickHouseClient.query)
    sql_param = sig.parameters["sql"]
    assert sql_param.kind == inspect.Parameter.POSITIONAL_ONLY
