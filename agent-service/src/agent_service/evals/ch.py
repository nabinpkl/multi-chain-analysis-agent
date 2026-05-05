"""Async ClickHouse client for the eval-probe layer.

Thin wrapper around `clickhouse-connect`'s `AsyncClient`. Exposes one
method, `query`, that REQUIRES every dynamic value to appear as a
`{name:Type}` placeholder bound by a kwarg. Values reach the
ClickHouse server via `param_<name>` URL args; the server binds them
as typed literals before parsing the SQL.

There is intentionally NO path through this wrapper that allows
Python-side string substitution of values. The `query` method's
positional-only `sql` arg + kwargs-only params signature means a
caller cannot accidentally pass `sql=f"...{x}..."` and feel clever.

The companion `clickhouse-connect` library does have a legacy
`finalize_query` mode that accepts `%(name)s` Python-side
substitution; we never expose that path. As long as callers stick
to `{name:Type}` placeholders bound via kwargs, server-side
parameterized queries handle SQL injection the same way JDBC
PreparedStatement / DB-API parameterized execute does.

Reads target the `otel.otel_traces` table written by the OTel
collector. We never write from this module.
"""

from __future__ import annotations

import os
from typing import Any

from clickhouse_connect import get_async_client
from clickhouse_connect.driver.asyncclient import AsyncClient


class ClickHouseClient:
    """Probe-side ClickHouse access. One async method, server-side
    parameterized queries only.
    """

    def __init__(self, client: AsyncClient) -> None:
        self._client = client

    @classmethod
    async def connect(
        cls,
        *,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        database: str | None = None,
    ) -> "ClickHouseClient":
        """Open a connection. Each argument falls back to the
        corresponding env var, then to a default that matches the
        local compose stack (multichain-clickhouse on port 8123, the
        `otel` database where the collector lands traces).

        Env vars: CLICKHOUSE_HOST, CLICKHOUSE_PORT, CLICKHOUSE_USER,
        CLICKHOUSE_PASSWORD, CLICKHOUSE_DB. Same names compose uses
        for the agent-service / api containers, so a `set -a; source
        .env` before `just eval` from the host shell is enough.
        """
        client = await get_async_client(
            host=host or os.environ.get("CLICKHOUSE_HOST", "localhost"),
            port=port or int(os.environ.get("CLICKHOUSE_PORT", "8123")),
            username=username or os.environ.get("CLICKHOUSE_USER", "default"),
            password=password if password is not None
                else os.environ.get("CLICKHOUSE_PASSWORD", ""),
            database=database or os.environ.get("CLICKHOUSE_DB_OTEL", "otel"),
        )
        return cls(client)

    async def query(self, sql: str, /, **params: Any) -> list[dict[str, Any]]:
        """Run a parameterized SELECT against ClickHouse.

        ALL dynamic values MUST appear as `{name:Type}` placeholders
        in `sql` and as keyword arguments here. Values are sent as
        `param_<name>` URL query args; the server binds them as
        typed literals before parsing the SQL.

        DO NOT pre-format `sql` with f-strings, `.format()`, or
        `%`-interpolation. Either the value is a literal in the SQL
        text (a hardcoded constant), or it is a `{name:Type}`
        placeholder bound by a kwarg here. There is no third option.

        The `/` makes `sql` positional-only so a caller cannot pass
        `sql=f"...{x}..."` and look syntactically clean while
        smuggling an interpolated value past the binding mechanism.
        """
        result = await self._client.query(sql, parameters=params)
        return [
            dict(zip(result.column_names, row, strict=True))
            for row in result.result_rows
        ]

    async def aclose(self) -> None:
        await self._client.close()
