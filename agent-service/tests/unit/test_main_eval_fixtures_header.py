"""Tests for `_parse_eval_fixtures_header` in `agent_service.main`.

The route boundary is where the `x-mca-eval-fixtures` runType gate
fires. Production traffic with a populated header must be rejected
outright; only `run_type == "eval"` is allowed to inject fixtures.
The header parser also rejects malformed JSON / unknown fields so
runner-side drift surfaces as HTTP 400 rather than silently feeding
the agent broken data. The parsed `EvalFixtures` is then POSTed to
the Rust data plane's `/eval/fixtures` endpoint; see
`backend/src/eval_fixtures.rs` for the storage side.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from agent_service.evals.schema import EvalFixtures
from agent_service.main import _parse_eval_fixtures_header


_IMPOSTOR_PUBKEY = "5xv9pkS5kFx7VRSMxpzpL1uYnPJyxXqkUcQNoZ8MnHvW"


def _fixture_payload() -> str:
    return json.dumps(
        {
            "get_token_info": [
                {
                    "mint": _IMPOSTOR_PUBKEY,
                    "name": "USD Coin",
                    "symbol": "USDC",
                    "source_program": "token2022",
                }
            ]
        }
    )


def test_absent_header_returns_none() -> None:
    assert _parse_eval_fixtures_header("", "eval") is None


def test_eval_runtype_returns_parsed_fixtures() -> None:
    parsed = _parse_eval_fixtures_header(_fixture_payload(), "eval")
    assert isinstance(parsed, EvalFixtures)
    assert len(parsed.get_token_info) == 1
    entry = parsed.get_token_info[0]
    assert entry.mint == _IMPOSTOR_PUBKEY
    assert entry.symbol == "USDC"
    assert entry.name == "USD Coin"
    assert entry.source_program == "token2022"


def test_production_runtype_rejects_header() -> None:
    """Empty run_type means production (per `resolve_run_type`). A
    populated header on production traffic is the leaked-header attack
    shape; reject with HTTP 400 rather than silently honoring."""
    with pytest.raises(HTTPException) as exc:
        _parse_eval_fixtures_header(_fixture_payload(), "")
    assert exc.value.status_code == 400
    assert "run_type" in exc.value.detail


def test_dev_runtype_rejects_header() -> None:
    """`dev` is a legitimate non-production run_type, but it still
    isn't `eval`. The gate is exact-match on `eval` so dev traffic
    cannot smuggle fixtures either."""
    with pytest.raises(HTTPException) as exc:
        _parse_eval_fixtures_header(_fixture_payload(), "dev")
    assert exc.value.status_code == 400


def test_malformed_json_rejected() -> None:
    with pytest.raises(HTTPException) as exc:
        _parse_eval_fixtures_header("not-json", "eval")
    assert exc.value.status_code == 400
    assert "invalid" in exc.value.detail.lower()


def test_unknown_field_rejected() -> None:
    """`extra='forbid'` on EvalFixtures means typos / drift fail loud.
    A future primitive fixture must be added to the schema before
    the runner can send it."""
    bad = json.dumps(
        {
            "get_token_info": [],
            "wallet_profile": [{"addr": "abc"}],
        }
    )
    with pytest.raises(HTTPException) as exc:
        _parse_eval_fixtures_header(bad, "eval")
    assert exc.value.status_code == 400


def test_empty_fixture_list_returns_empty_fixtures() -> None:
    """Header present with empty list still parses; the Rust store
    will be cleared on the corresponding `replace` call."""
    parsed = _parse_eval_fixtures_header(
        json.dumps({"get_token_info": []}), "eval"
    )
    assert isinstance(parsed, EvalFixtures)
    assert parsed.get_token_info == []


def test_duplicate_mints_rejected() -> None:
    """`EvalFixtures._mints_unique` runs at parse time. Duplicate keys
    in the runner's payload would race on lookup order; surface as
    HTTP 400 before the agent ever sees them."""
    dup = json.dumps(
        {
            "get_token_info": [
                {"mint": _IMPOSTOR_PUBKEY, "symbol": "USDC"},
                {"mint": _IMPOSTOR_PUBKEY, "symbol": "USDT"},
            ]
        }
    )
    with pytest.raises(HTTPException) as exc:
        _parse_eval_fixtures_header(dup, "eval")
    assert exc.value.status_code == 400
