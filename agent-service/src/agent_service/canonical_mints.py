"""Hardcoded allow-list of canonical SPL mint pubkeys.

Solana SPL transfers carry the mint pubkey directly  every token
account is mint-pinned at creation and every Transfer / TransferChecked
references the pubkey, never a symbol. The pubkey is the forge-proof
identifier. The on-chain `name` / `symbol` / `uri` (Metaplex or
Token-2022 metadata) are attacker-controlled strings: anyone can
create a mint with `name="USD Coin"` and `symbol="USDC"`.

This registry maps the canonical pubkey for a small set of blue-chip
tokens to display strings we control. `get_token_info` consults it
and stamps `verified: bool` plus `canonical_name` / `canonical_symbol`
on the payload it returns to the model. The system prompt teaches
the model to prefer canonical fields when verified and to qualify
the on-chain symbol as unverified otherwise.

The on-chain `name` / `symbol` / `uri` pass through unchanged so the
model retains the forensic surface; the verified flag is a tag, not
a filter.

Curation policy:
- Add entries by PR review only. This is a domain constant, not env
  config; the canonical USDC mint is the same in every deployment.
- Keep the set small. The point is "we have stood behind the
  identity of this pubkey," not "we have heard of this token."
- LSTs (JitoSOL, mSOL, bSOL) and majors (JUP, BONK, PYTH, WIF) are
  intentionally deferred until an eval shows a concrete
  narrative-quality miss on one of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final


@dataclass(frozen=True)
class CanonicalToken:
    """One row in the canonical-mint registry."""

    mint: str
    canonical_name: str
    canonical_symbol: str


CANONICAL_MINTS: Final[dict[str, CanonicalToken]] = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": CanonicalToken(
        mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        canonical_name="USD Coin",
        canonical_symbol="USDC",
    ),
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": CanonicalToken(
        mint="Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        canonical_name="Tether USD",
        canonical_symbol="USDT",
    ),
    "So11111111111111111111111111111111111111112": CanonicalToken(
        mint="So11111111111111111111111111111111111111112",
        canonical_name="Wrapped SOL",
        canonical_symbol="wSOL",
    ),
}


def lookup(mint: str) -> CanonicalToken | None:
    """Return the canonical record for `mint`, or None if unknown."""
    return CANONICAL_MINTS.get(mint)


def stamp_verification(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a new payload with `verified` plus `canonical_*` added.

    `verified` is True iff `payload["mint"]` is in the registry. When
    True, `canonical_name` and `canonical_symbol` are populated from
    the registry entry. When False, no canonical_* keys are added.

    The on-chain `name` / `symbol` / `uri` fields are passed through
    unchanged. The new fields are our hardcoded data, not external
    text; they survive `sanitize_token_info_payload` redaction by
    design so the model still gets the canonical label when the
    external-text channel is off.
    """
    mint = payload.get("mint", "")
    canonical = CANONICAL_MINTS.get(mint) if isinstance(mint, str) else None
    out = {**payload, "verified": canonical is not None}
    if canonical is not None:
        out["canonical_name"] = canonical.canonical_name
        out["canonical_symbol"] = canonical.canonical_symbol
    return out
