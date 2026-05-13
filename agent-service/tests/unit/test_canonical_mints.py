"""Tests for `canonical_mints.stamp_verification` and its interaction
with `sanitize_token_info_payload`.

The verification stamp is a display-layer defense: the mint pubkey is
the forge-proof identity, but the on-chain `name` / `symbol` / `uri`
are attacker-controlled (anyone can mint a Token-2022 with
`symbol="USDC"`). `stamp_verification` tags whether a particular
pubkey is one we stand behind so the prompt's `token_verification`
rule can have the model qualify unverified labels.
"""

from __future__ import annotations

from agent_service.boundary import sanitize_token_info_payload
from agent_service.canonical_mints import (
    CANONICAL_MINTS,
    CanonicalToken,
    stamp_verification,
)


_CANONICAL_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# Deliberately non-registry pubkey. Real-looking base58, no
# significance beyond "not in CANONICAL_MINTS".
_IMPOSTOR_PUBKEY = "5xv9pkS5kFx7VRSMxpzpL1uYnPJyxXqkUcQNoZ8MnHvW"


def test_lookup_canonical_usdc_returns_record() -> None:
    record = CANONICAL_MINTS[_CANONICAL_USDC]
    assert isinstance(record, CanonicalToken)
    assert record.canonical_symbol == "USDC"
    assert record.canonical_name == "USD Coin"


def test_lookup_unknown_returns_none() -> None:
    assert CANONICAL_MINTS.get(_IMPOSTOR_PUBKEY) is None


def test_get_token_info_canonical_usdc_stamps_verified() -> None:
    payload = {
        "mint": _CANONICAL_USDC,
        "name": "USD Coin",
        "symbol": "USDC",
        "uri": "",
        "update_authority": "2wmVCSfPxGPjrnMMn7rchp4uaeoTqN39mXFC2zhPdri9",
        "source_program": "metaplex",
        "found": True,
    }
    stamped = stamp_verification(payload)
    assert stamped["verified"] is True
    assert stamped["canonical_symbol"] == "USDC"
    assert stamped["canonical_name"] == "USD Coin"
    # On-chain fields pass through unchanged.
    assert stamped["name"] == "USD Coin"
    assert stamped["symbol"] == "USDC"
    assert stamped["mint"] == _CANONICAL_USDC


def test_get_token_info_impostor_stamps_unverified() -> None:
    payload = {
        "mint": _IMPOSTOR_PUBKEY,
        "name": "USD Coin",
        "symbol": "USDC",
        "uri": "ipfs://bafy.../meta.json",
        "update_authority": _IMPOSTOR_PUBKEY,
        "source_program": "token2022",
        "found": True,
    }
    stamped = stamp_verification(payload)
    assert stamped["verified"] is False
    assert "canonical_name" not in stamped
    assert "canonical_symbol" not in stamped
    # Attacker-controlled fields survive unscrubbed: the model still
    # sees them, just with the `verified: false` flag that the prompt
    # rule uses to qualify the mention.
    assert stamped["name"] == "USD Coin"
    assert stamped["symbol"] == "USDC"


def test_get_token_info_sanitization_preserves_canonical() -> None:
    """When the external-text channel is off, on-chain strings get
    redacted but the canonical labels and verified flag pass through
    because they are our hardcoded data, not external text."""
    stamped = stamp_verification(
        {
            "mint": _CANONICAL_USDC,
            "name": "USD Coin",
            "symbol": "USDC",
            "uri": "https://circle.example/usdc.json",
            "update_authority": "2wmVCSfPxGPjrnMMn7rchp4uaeoTqN39mXFC2zhPdri9",
            "source_program": "metaplex",
            "found": True,
        }
    )
    redacted = sanitize_token_info_payload(stamped)
    assert redacted["verified"] is True
    assert redacted["canonical_symbol"] == "USDC"
    assert redacted["canonical_name"] == "USD Coin"
    assert redacted["name"] != "USD Coin"
    assert redacted["symbol"] != "USDC"
    assert redacted["uri"] != "https://circle.example/usdc.json"


def test_get_token_info_no_metadata_stamps_unverified() -> None:
    payload = {
        "mint": _IMPOSTOR_PUBKEY,
        "name": "",
        "symbol": "",
        "uri": "",
        "update_authority": "",
        "source_program": "",
        "found": False,
    }
    stamped = stamp_verification(payload)
    assert stamped["verified"] is False
    assert "canonical_symbol" not in stamped
