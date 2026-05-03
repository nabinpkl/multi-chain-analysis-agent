"""Health endpoint smoke. Trivial but worth having: catches accidental
removal of the route during refactors, and validates that the
FastAPI app can boot under TestClient (which runs the lifespan
handler, including PrimitiveClient construction)."""

from __future__ import annotations


def test_health_returns_ok(test_app):
    resp = test_app.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
