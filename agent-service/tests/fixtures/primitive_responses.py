"""Backward-compat re-export shim. The canonical home for these
canned primitive responses + binary proto encoders is
`agent_service.test_support.primitive_responses`, which lives under
`src/` so the hermetic-eval mock service at
`evals/cases-hermetic/mock-service/` can import them without a
`tests/` cross-package dependency.

Existing test modules still import from this path. After the
mock substrate lands and stabilizes, this shim is a follow-up
delete in one commit (per the plan's "encoder refactor cleanup"
follow-on)."""

from __future__ import annotations

from agent_service.test_support.primitive_responses import *  # noqa: F401, F403
from agent_service.test_support.primitive_responses import (  # noqa: F401
    COMMUNITY_SUMMARY_RESPONSE,
    GET_TOKEN_INFO_USDC_RESPONSE,
    SNAPSHOT_BEGIN_RESPONSE,
    SNAPSHOT_GONE_ERROR,
    VALID_SNAPSHOT_ID,
    WALLET_NOT_IN_WINDOW_ERROR,
    WALLET_PROFILE_ADDR,
    WALLET_PROFILE_COMMUNITY_ID,
    WALLET_PROFILE_RESPONSE,
    encode_community_summary_response,
    encode_get_token_info_response,
    encode_snapshot_begin_response,
    encode_wallet_profile_response,
    make_ask_payload,
)
