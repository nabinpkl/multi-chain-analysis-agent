"""Shared fixtures for eval unit tests.

`FakeChClient` lets probe tests inject canned ClickHouse rows
without standing up the real driver. It satisfies the same
`query(sql, **params) -> list[dict]` interface as the real
`ClickHouseClient` so probes consume it identically.
"""

from __future__ import annotations

from typing import Any, Callable


class FakeChClient:
    """In-process stand-in for ClickHouseClient. Tests supply a
    `respond_with` callable that takes (sql, params) and returns
    the rows the probe should see; default returns an empty list.

    Records every call into `.calls` so tests can assert the probe
    hit the SQL it should have."""

    def __init__(
        self,
        respond_with: Callable[[str, dict[str, Any]], list[dict[str, Any]]] | None = None,
    ) -> None:
        self._respond = respond_with or (lambda _sql, _p: [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query(self, sql: str, /, **params: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        return self._respond(sql, params)

    async def aclose(self) -> None:
        pass


def fake_ch(rows: list[dict[str, Any]] | None = None) -> FakeChClient:
    """Convenience: build a FakeChClient that always returns the
    same canned rows regardless of SQL. Use for probes whose query
    is fixed; for branchy probes drive `respond_with` directly."""
    rows = rows or []
    return FakeChClient(respond_with=lambda _sql, _p: rows)
