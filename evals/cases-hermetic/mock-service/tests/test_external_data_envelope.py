"""Structural canary for the `<external_data>` envelope on the mock's
MCP read tools. Pairs with
`backend/src/mcp.rs::tests::tool_result_external_data_text_carries_envelope`
on the upstream side: this side guarantees the mock stays faithful to
what the live Rust MCP emits, so a hermetic run can't accidentally
pass with the defense silently removed.

Why this test exists: `wallet_profile` and `community_summary` do not
have natural attacker-controlled free-text fields (`role` is an enum,
counterparty `addr` is base58, etc.), so a behavioral injection probe
on those tools is artificial. Asserting the envelope is present at the
wire layer is the honest structural pin.
"""

from __future__ import annotations

import json

from eval_mock.mcp_proxy import (
    _EXTERNAL_DATA_TOOLS,
    wrap_external_data,
)


def test_external_data_tool_set_covers_all_read_tools() -> None:
    # If a future tool joins the read-side surface (e.g. a `memos`
    # primitive), adding it to `_DISPATCH` without also adding it
    # here would silently leave that tool's response outside the
    # envelope. Forcing the set membership to be explicit in code
    # makes the omission visible at review time.
    assert _EXTERNAL_DATA_TOOLS == {
        "wallet_profile",
        "community_summary",
        "get_token_info",
    }


def test_wrap_external_data_matches_python_boundary_format() -> None:
    # Byte-for-byte mirror of `agent_service.boundary.wrap_external_data`
    # output so the codex MCP path and the pydantic-ai HTTP path
    # produce identical-shaped envelopes.
    s = wrap_external_data("wallet_profile", {"addr": "abc", "role": "X"})
    assert (
        s
        == '<external_data primitive="wallet_profile">\n'
        '{"addr":"abc","role":"X"}\n'
        "</external_data>"
    )


def test_wrap_external_data_uses_compact_json() -> None:
    # Pretty-printed JSON would diverge byte-wise from the Rust side
    # (`backend/src/mcp.rs::wrap_external_data` uses
    # `serde_json::to_string` which is compact by default). The
    # Python side has historically defaulted to indented JSON via
    # `json.dumps` without separators; pinning the separators here
    # catches a regression on either side.
    s = wrap_external_data("community_summary", {"a": 1, "b": [1, 2]})
    assert '{"a":1,"b":[1,2]}' in s
    assert ", " not in s
    assert ": " not in s


def test_wrap_external_data_round_trip_yields_original_payload() -> None:
    # The envelope is just a string wrapper; clients (or a human
    # debugging a trace) should be able to extract the body between
    # the two tags and JSON-parse it back to the original payload.
    payload = {"top_counterparties": [{"addr": "abc", "volume": 1.5}]}
    s = wrap_external_data("wallet_profile", payload)
    opener = '<external_data primitive="wallet_profile">\n'
    closer = "\n</external_data>"
    assert s.startswith(opener) and s.endswith(closer)
    body = s[len(opener): -len(closer)]
    assert json.loads(body) == payload
