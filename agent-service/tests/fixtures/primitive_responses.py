"""Canned Rust-side responses captured from real `/primitive/*` and
`/turn/*` calls in Phase A. Used by tests to replay against the
mocked httpx client without needing a live Rust container.

The dicts here mirror the typed primitive output shape (the `value`
field of the proto envelope). Stage 2 of the proto migration added
`encode_*` helpers that pack these into a binary proto
`PrimitiveResponseEnvelope` for the binary wire mocks. Tests should
prefer the `encode_*` byte-emitters; raw dict access is kept for
tests that need to inspect specific fields.

If the Rust wire shape changes, regenerate by capturing fresh
responses with curl against a running Rust API and updating the
dicts below. Schema parity with the proto source-of-truth is
enforced by the `encode_*` helpers raising on extra/missing fields.
"""

from __future__ import annotations

from google.protobuf import json_format, struct_pb2

from multichain.wire.shared.v1 import (
    get_token_info_pb2 as gti_pb,
    primitive_envelope_pb2 as env_pb,
    provenance_pb2 as prov_pb,
    snapshot_pb2 as snap_pb,
)


# ---------------------------------------------------------------------------
# AgentRequest helpers (Phase I locked the full ViewContext shape)
# ---------------------------------------------------------------------------


# Production preset for `AgentSwitches` (every defense + every channel
# ON). The proto defines proto3 false defaults as deliberately unsafe
# per `switches.proto` so a caller forgetting a field gets a noticeable
# regression rather than a silently-leaky agent. Tests want the
# realistic prod path: defenses on, no per-turn `build_agent` rebuild
# (which `drops_from_switches` triggers when any defend_* flag is off
# and would bypass any `app.state.handles.primary_agent` overrides
# tests apply by replacing the agent with a freshly-constructed one).
_PRODUCTION_SWITCHES: dict = {
    "stayInRole": {
        "defendChatTemplateSpoofing": True,
        "defendConstitutionJudge": True,
        "defendPersonaSwap": True,
        "defendDecodeAndExecute": True,
        "defendIdentityReveal": True,
        "defendOffDomain": True,
        "defendMemoInjection": True,
    },
    "dontFabricate": True,
    "crossCheck": {
        "paraphraseAwareMatch": True,
        "groundTruthMatch": True,
    },
    "dontRepeatYourself": True,
    "channels": {
        "narrativeOutputEnabled": True,
        "externalTextInputEnabled": True,
    },
}


def make_ask_payload(
    user_question: str = "Profile this wallet",
    focus_addr: str | None = None,
    *,
    thread_id: str | None = None,
    show_trace: bool = False,
) -> dict:
    """Build the JSON body for `POST /agent/ask` matching the proto
    `AgentRequest` canonical JSON shape (camelCase, EntityRef oneof
    as `{"wallet":{"id":...}}`). Tests pass through this helper so a
    future shape change updates one place, not 30 call sites.

    Switches default to the production preset (every defense and
    channel ON). Tests that need to exercise specific off-states
    construct the payload directly rather than going through this
    helper.
    """
    addr = focus_addr if focus_addr is not None else WALLET_PROFILE_ADDR
    payload: dict = {
        "userQuestion": user_question,
        "context": {
            "liveWindowSecs": 60,
            "focus": {"wallet": {"id": addr}},
            "selection": [],
        },
        "switches": _PRODUCTION_SWITCHES,
        "showTrace": show_trace,
    }
    if thread_id is not None:
        payload["threadId"] = thread_id
    return payload

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
        # Proto enum canonical JSON form: full upper-snake name with prefix.
        "role": "NODE_ROLE_WHALE",
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


# ---------------------------------------------------------------------------
# Binary proto encoders. Stage 2: production wire is binary protobuf;
# mocks return bytes. The dict shapes above stay readable; helpers below
# pack them into the proto envelope and serialize.
#
# `value` becomes a `google.protobuf.Struct` populated from the dict,
# matching what the Rust handler does (typed proto output -> serde_json
# -> Struct on the way out).
# ---------------------------------------------------------------------------


def _provenance_dict_to_proto(p: dict) -> prov_pb.ProvenanceRef:
    """Convert one provenance dict (legacy externally-tagged JSON form
    with `kind` discriminator) into the proto oneof shape."""
    kind = p["kind"]
    ref = prov_pb.ProvenanceRef()
    if kind == "wallet":
        ref.wallet.addr = p["addr"]
        if p.get("idx") is not None:
            ref.wallet.idx = p["idx"]
    elif kind == "edge":
        ref.edge.id = p["id"]
        ref.edge.src = p["src"]
        ref.edge.dst = p["dst"]
    elif kind == "community":
        ref.community.id = p["id"]
    elif kind == "time-range" or kind == "time_range":
        ref.time_range.from_s = p["from_s"]
        ref.time_range.to_s = p["to_s"]
    elif kind == "number":
        ref.number.metric = p["metric"]
        ref.number.value = p["value"]
        ref.number.support.extend(p.get("support", []))
    else:
        raise ValueError(f"unknown provenance kind: {kind!r}")
    return ref


def _envelope_bytes(value_dict: dict, provenance_dicts: list[dict]) -> bytes:
    env = env_pb.PrimitiveResponseEnvelope()
    # google.protobuf.Struct loaded from a Python dict via json_format.
    # Same serialization rules as serde_json on the Rust side.
    struct = struct_pb2.Struct()
    json_format.ParseDict(value_dict, struct)
    env.value.CopyFrom(struct)
    for p in provenance_dicts:
        env.provenance.append(_provenance_dict_to_proto(p))
    return env.SerializeToString()


def encode_wallet_profile_response() -> bytes:
    """Binary proto envelope for the canned wallet_profile response."""
    return _envelope_bytes(
        WALLET_PROFILE_RESPONSE["value"],
        WALLET_PROFILE_RESPONSE["provenance"],
    )


def encode_community_summary_response() -> bytes:
    """Binary proto envelope for the canned community_summary response."""
    return _envelope_bytes(
        COMMUNITY_SUMMARY_RESPONSE["value"],
        COMMUNITY_SUMMARY_RESPONSE["provenance"],
    )


GET_TOKEN_INFO_USDC_RESPONSE = {
    "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "name": "USD Coin",
    "symbol": "USDC",
    "uri": "",
    "update_authority": "2wmVCSfPxGPjrnMMn7rchp4uaeoTqN39mXFC2zhPdri9",
    "source_program": "metaplex",
    # Verified / canonical_* are stamped server-side by Rust's
    # `canonical_mints::stamp_verification`. Tests mock the Rust HTTP
    # boundary, so the canned bytes must include the stamped fields
    # the live route would have emitted. Default is the canonical
    # USDC stamp; impostor fixtures override to verified=False with
    # no canonical_* fields.
    "verified": True,
    "canonical_name": "USD Coin",
    "canonical_symbol": "USDC",
}


def encode_get_token_info_response(payload: dict | None = None) -> bytes:
    """Binary `GetTokenInfoOutput` for the canned token-info response.
    Default payload is the USDC happy-path. Tests that want a different
    shape (e.g. a clearly-injection-shaped name) pass their own dict.

    `canonical_name` and `canonical_symbol` are optional in the proto;
    omitted dict keys map to absent proto fields, so impostor fixtures
    can drop them to mimic the server-side stamp for an unverified
    mint."""
    p = payload or GET_TOKEN_INFO_USDC_RESPONSE
    kwargs = dict(
        mint=p["mint"],
        name=p["name"],
        symbol=p["symbol"],
        uri=p["uri"],
        update_authority=p["update_authority"],
        source_program=p["source_program"],
        verified=p.get("verified", False),
    )
    if p.get("canonical_name") is not None:
        kwargs["canonical_name"] = p["canonical_name"]
    if p.get("canonical_symbol") is not None:
        kwargs["canonical_symbol"] = p["canonical_symbol"]
    out = gti_pb.GetTokenInfoOutput(**kwargs)
    return out.SerializeToString()


def encode_snapshot_begin_response() -> bytes:
    msg = snap_pb.SnapshotBeginResponse(
        snapshot_id=SNAPSHOT_BEGIN_RESPONSE["snapshot_id"],
        expires_at_ms=SNAPSHOT_BEGIN_RESPONSE["expires_at_ms"],
        window_secs=SNAPSHOT_BEGIN_RESPONSE["window_secs"],
    )
    return msg.SerializeToString()
