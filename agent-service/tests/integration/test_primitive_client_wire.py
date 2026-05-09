"""Wire-level integration smoke. Talks to the REAL Rust internal
listener over binary protobuf. Skipped automatically when the API is
not reachable, so it never blocks CI runs that don't have docker
compose up.

The internal listener is NOT host-published (see
`backend/src/api/mod.rs::internal_router` and the docker-compose `api`
service block). Run this from inside the docker compose network:

    docker compose up -d --build api
    docker compose exec agent-service uv run pytest \\
        tests/integration/test_primitive_client_wire.py -v

Override `LIVE_RUST_URL` if you want to point at a host-exposed port
during one-off debugging.

This complements `test_snapshot_lease.py` (which uses pytest-httpx
mocks) by exercising the actual binary wire end-to-end:

- `Content-Type: application/x-protobuf` round-trips on the request side
- `Accept: application/x-protobuf` round-trips on the response side
- Buffa-encoded request bytes are decodable by the Rust side, and the
  proto-encoded response bytes are decodable here.
- Snapshot lease lifecycle works against a non-mocked snapshot cache.
- Errors come back JSON-shaped (Rust's design) and parse correctly.
"""

from __future__ import annotations

import os

import httpx
import pytest

from agent_service.primitive_client import PrimitiveClient, PrimitiveError

LIVE_URL = os.environ.get("LIVE_RUST_URL", "http://api:8004")


def _api_alive() -> bool:
    try:
        r = httpx.get(f"{LIVE_URL}/health", timeout=1.0)
        return r.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _api_alive(),
    reason=f"Rust API not reachable at {LIVE_URL}; "
    "run `docker compose up -d --build api` to enable.",
)


@pytest.fixture
async def live_client():
    client = PrimitiveClient(base_url=LIVE_URL)
    try:
        yield client
    finally:
        await client.close()


async def test_turn_lifecycle_against_live_rust(live_client):
    lease = await live_client.begin_turn()
    assert lease.snapshot_id  # ulid-shaped string
    assert lease.window_secs == 60
    assert lease.expires_at_ms > 0
    # Idempotent end; should not raise.
    await live_client.end_turn(lease.snapshot_id)


async def test_wallet_profile_not_in_window_against_live_rust(live_client):
    """Bogus addr must come back as a typed `not_in_window` error
    (Rust 404, JSON body), proving the binary->error path works.
    Uses a base58 "Z..." string that's syntactically valid but
    cosmically unlikely to ever appear on chain."""
    lease = await live_client.begin_turn()
    try:
        with pytest.raises(PrimitiveError) as excinfo:
            await live_client.wallet_profile(
                addr="ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ",
                snapshot_id=lease.snapshot_id,
            )
        assert excinfo.value.kind == "not_in_window"
        assert excinfo.value.status_code == 404
    finally:
        await live_client.end_turn(lease.snapshot_id)


async def test_snapshot_gone_against_live_rust(live_client):
    """Bogus snapshot_id must return 410 Gone with kind=snapshot_gone."""
    with pytest.raises(PrimitiveError) as excinfo:
        await live_client.wallet_profile(
            addr="11111111111111111111111111111111",
            snapshot_id="DOES_NOT_EXIST",
        )
    assert excinfo.value.kind == "snapshot_gone"
    assert excinfo.value.status_code == 410
