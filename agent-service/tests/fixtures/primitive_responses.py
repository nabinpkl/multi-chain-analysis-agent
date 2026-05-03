"""Canned Rust-side responses captured from real `/primitive/*` and
`/turn/*` calls in Phase A. Used by tests to replay against the
mocked httpx client without needing a live Rust container.

If the Rust wire shape changes, regenerate by:

    1. Run the Phase A end-to-end probe in `noble-orbiting-key.md`
    2. Save the response bodies here verbatim
    3. Re-run `cd agent-service && uv run pytest`

The shapes are pydantic-validated by `unit/test_wire_shapes.py`, so
drift surfaces as a test failure, not silent rot.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Snapshot lease
# ---------------------------------------------------------------------------

VALID_SNAPSHOT_ID = "01KQNJEN2XA64S7Q0PBD6KW8ZY"

SNAPSHOT_BEGIN_RESPONSE: dict = {
    "snapshot_id": VALID_SNAPSHOT_ID,
    "expires_at_ms": 1777767016509,
    "window_secs": 60,
}

SNAPSHOT_GONE_ERROR: dict = {
    "error": f"snapshot_id {VALID_SNAPSHOT_ID} not found or expired",
    "kind": "snapshot_gone",
}

# ---------------------------------------------------------------------------
# wallet_profile (Phase A captured response, real grounded values)
# ---------------------------------------------------------------------------

WALLET_PROFILE_ADDR = "DLZSeiq2xjikgwcniQB6B89uodkbQHrTcco6mJu9UNuK"
WALLET_PROFILE_COMMUNITY_ID = 8

WALLET_PROFILE_RESPONSE: dict = {
    "value": {
        "addr": WALLET_PROFILE_ADDR,
        "role": "whale",
        "community_id": WALLET_PROFILE_COMMUNITY_ID,
        "stats": {
            "degree": 5,
            "total_volume_lamports": 80223943444.0,
            "in_volume_lamports": 80223943444.0,
            "out_volume_lamports": 0.0,
            "bidir_volume_lamports": 0.0,
            "sol_degree": 5,
            "spl_degree": 0,
        },
        "top_counterparties": [
            {"addr": "Fe3JqpnvMZs7Y5b9soAtPAkjgkjJUQwDM8LjRiSJjMZU", "volume": 50000000000.0},
            {"addr": "B3uDBS6gSnuzCEoSCGbup4JvG4PghYNY6aLoKsksDq8N", "volume": 20000000000.0},
            {"addr": "Gygj9QQby4j2jryqyqBHvLP7ctv2SaANgh4sCb69BUpA", "volume": 5000000000.0},
            {"addr": "A4ZGFWQupQnxQDkFTKGq4TF2LSvPwUnvX2y6WE5rmHDA", "volume": 3000000000.0},
            {"addr": "CT5WRRtZxsoVRBHc6art6HWrM4azWo4ofuiT853PtJTc", "volume": 2223943444.0},
        ],
        "age_in_window_secs": 0,
    },
    "provenance": [
        {"kind": "wallet", "addr": WALLET_PROFILE_ADDR, "idx": 0},
        {"kind": "wallet", "addr": "Fe3JqpnvMZs7Y5b9soAtPAkjgkjJUQwDM8LjRiSJjMZU", "idx": 1},
        {"kind": "community", "id": WALLET_PROFILE_COMMUNITY_ID},
        {
            "kind": "number",
            "metric": "total_volume_lamports",
            "value": 80223943444.0,
            "support": [WALLET_PROFILE_ADDR],
        },
    ],
    "subgraph_slice": None,
}

WALLET_NOT_IN_WINDOW_ERROR: dict = {
    "error": f"wallet not in current live window: {WALLET_PROFILE_ADDR}",
    "kind": "not_in_window",
}

# ---------------------------------------------------------------------------
# community_summary
# ---------------------------------------------------------------------------

COMMUNITY_SUMMARY_RESPONSE: dict = {
    "value": {
        "community_id": WALLET_PROFILE_COMMUNITY_ID,
        "size": 7,
        "total_volume": 23547094862369.0,
        "internal_volume": 23547094862369.0,
        "external_volume": 80223943444.0,
        "edge_count": 6,
        "top_wallets": [
            {"addr": WALLET_PROFILE_ADDR, "degree": 5, "volume": 80223943444.0},
            {"addr": "Fe3JqpnvMZs7Y5b9soAtPAkjgkjJUQwDM8LjRiSJjMZU", "degree": 3, "volume": 50000000000.0},
        ],
    },
    "provenance": [
        {"kind": "community", "id": WALLET_PROFILE_COMMUNITY_ID},
        {"kind": "wallet", "addr": WALLET_PROFILE_ADDR, "idx": None},
        {
            "kind": "number",
            "metric": "size",
            "value": 7.0,
            "support": [WALLET_PROFILE_ADDR],
        },
    ],
    "subgraph_slice": None,
}
