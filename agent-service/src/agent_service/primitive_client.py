"""HTTP client wrapping the Rust data plane on :8002. Phase 0 only
wires `wallet_profile`; Phase A adds the snapshot-lease round-trip
(`/turn/begin`, `/turn/end`) and propagates `snapshot_id` on every call.

The client is a thin shim over `httpx.AsyncClient`. The pydantic models
mirror the Rust wire types defined in
`backend/src/agent/primitives/wallet_profile.rs`. In Phase A these are
auto-generated from `backend/src/wire/shared.rs` via the
`datamodel-code-generator` recipe, replacing the hand-written shapes
here.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Wire types mirroring Rust (Phase 0 hand-write; Phase A auto-generates).
# ---------------------------------------------------------------------------


class _LiveScope(BaseModel):
    """`TimeScope::Live` serializes as the bare string `"live"` in Rust
    via `#[serde(rename_all = "kebab-case")]` on a unit variant."""


class WalletProfileInput(BaseModel):
    addr: str
    # Rust externally-tagged enum: `Live` -> `"live"`, `Range` -> dict.
    # Phase 0 only sends Live.
    time_scope: Literal["live"] = "live"


class NodeStatsWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    degree: int
    total_volume_lamports: float
    in_volume_lamports: float
    out_volume_lamports: float
    bidir_volume_lamports: float
    sol_degree: int
    spl_degree: int


class TopCounterparty(BaseModel):
    model_config = ConfigDict(extra="forbid")

    addr: str
    volume: float


class WalletProfileOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    addr: str
    role: str | None
    community_id: int | None
    stats: NodeStatsWire
    top_counterparties: list[TopCounterparty]
    age_in_window_secs: int


class PrimitiveResponse(BaseModel):
    """Envelope every `/primitive/*` route returns. Mirrors
    `backend/src/api/primitives.rs::PrimitiveResponse`."""

    model_config = ConfigDict(extra="forbid")

    value: dict[str, Any]
    provenance: list[dict[str, Any]]
    subgraph_slice: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PrimitiveError(Exception):
    """Mirror of Rust `PrimitiveError`. `kind` matches the JSON key
    returned by `error_response()` so the agent can branch."""

    def __init__(self, kind: str, message: str, status_code: int):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class PrimitiveClient:
    """Wraps an `httpx.AsyncClient` pointed at the Rust data plane.

    The client itself is synchronous to construct; all I/O is async.
    Caller owns the lifecycle: build once at app startup, close on
    shutdown.
    """

    def __init__(self, base_url: str, timeout_s: float = 30.0):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_s),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def wallet_profile(self, addr: str) -> WalletProfileOutput:
        """Phase 0 Live-only call. Phase A adds `snapshot_id`."""
        body = WalletProfileInput(addr=addr).model_dump()
        resp = await self._client.post("/primitive/wallet_profile", json=body)
        if resp.status_code >= 400:
            self._raise_from_error(resp)
        envelope = PrimitiveResponse.model_validate(resp.json())
        return WalletProfileOutput.model_validate(envelope.value)

    @staticmethod
    def _raise_from_error(resp: httpx.Response) -> None:
        """Map Rust's `{ "error": ..., "kind": ... }` body to a
        `PrimitiveError`. Falls back to text body if the response is
        not JSON or doesn't carry the expected shape."""
        try:
            payload = resp.json()
            kind = str(payload.get("kind", "internal"))
            message = str(payload.get("error", resp.text))
        except Exception:
            kind = "internal"
            message = resp.text
        log.warning(
            "primitive_call_failed",
            status=resp.status_code,
            kind=kind,
            message=message,
        )
        raise PrimitiveError(kind=kind, message=message, status_code=resp.status_code)
