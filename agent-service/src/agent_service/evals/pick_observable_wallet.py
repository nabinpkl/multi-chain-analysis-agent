"""Pick a wallet currently observable in the live window.

Background: eval cases that exercise `wallet_profile` pin a specific
wallet address that's "observable in the live window" (i.e. has
recent transfer activity). The live window is a rolling 60-second
(or larger) slice of mainnet, so any pinned wallet eventually ages
out and the suite starts failing on "wallet not in current live
window" through no fault of the agent code. The fix is to refresh
the pinned address on drift.

This script queries the data plane's
`/graph/observable_wallets` endpoint (see
`backend/src/api/observable_wallets.rs`) and prints the top-degree
candidates currently visible. Use it before re-minting an eval
baseline that depends on a live-window wallet:

    just eval-pick-wallet            # print top 5
    just eval-pick-wallet --limit 1  # print one address only

Stdout is intended for shell pipes (e.g. `$(just eval-pick-wallet
--addr-only)`); errors go to stderr with non-zero exit.

Why a separate script (not part of the eval runner): the runner
shouldn't silently swap inputs out from under the case YAML. The
case is the contract; this helper makes refreshing the contract
mechanical. The operator still copy-pastes the address into the
yaml and re-mints the baseline so the diff is reviewable.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import httpx


_DEFAULT_BASE_URL_ENV = "DATA_PLANE_PUBLIC_URL"
_DEFAULT_BASE_URL = "http://localhost:8002"


def _resolve_base_url() -> str:
    return os.environ.get(_DEFAULT_BASE_URL_ENV) or _DEFAULT_BASE_URL


def fetch(base_url: str, *, window_secs: int, limit: int) -> dict[str, Any]:
    """Hit `/graph/observable_wallets` and return the parsed JSON.

    Raises:
        httpx.HTTPError on transport / non-2xx.
    """
    url = f"{base_url.rstrip('/')}/graph/observable_wallets"
    params = {"window": str(window_secs), "limit": str(limit)}
    r = httpx.get(url, params=params, timeout=10.0)
    r.raise_for_status()
    return r.json()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--base-url",
        default=_resolve_base_url(),
        help=(
            f"Data-plane public URL. Defaults to ${_DEFAULT_BASE_URL_ENV} "
            f"or {_DEFAULT_BASE_URL}."
        ),
    )
    p.add_argument(
        "--window",
        type=int,
        default=60,
        help="Live window in seconds (default: 60).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Number of candidates to return (default: 5).",
    )
    p.add_argument(
        "--addr-only",
        action="store_true",
        help=(
            "Print only the top-degree wallet's base58 address (no "
            "headers, no rank suffix). Suitable for shell capture."
        ),
    )
    args = p.parse_args(argv)

    try:
        payload = fetch(
            args.base_url, window_secs=args.window, limit=args.limit
        )
    except httpx.HTTPError as e:
        print(f"observable_wallets fetch failed: {e}", file=sys.stderr)
        return 2

    wallets = payload.get("wallets") or []
    if not wallets:
        print(
            "no observable wallets in current window; ingestion may be "
            "stalled or the live window may be empty",
            file=sys.stderr,
        )
        return 1

    if args.addr_only:
        # Top-1 only, no decoration; suitable for `WALLET=$(...)`.
        print(wallets[0]["addr"])
        return 0

    print(
        f"window={payload.get('window_secs')}s "
        f"latest_block_time={payload.get('latest_block_time')}"
    )
    for i, w in enumerate(wallets, 1):
        print(f"  {i}. {w['addr']}  (degree={w['degree_in_window']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
