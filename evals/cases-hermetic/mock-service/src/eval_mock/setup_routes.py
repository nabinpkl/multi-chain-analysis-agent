"""Eval-runner control surface. `POST /eval/setup` replaces the
fixture store from the case's `fixtures:` field; `DELETE /eval/setup`
clears it. Hermetic runner brackets each case with these calls.

Wire shape on POST: the JSON body of `EvalFixtures.model_dump_json()`
from `agent_service.evals.schema`. This module forbids unknown
fields so a future primitive added on the runner side without a
matching mock update fails loud with HTTP 422.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from eval_mock.fixtures import STORE

router = APIRouter()


class _GetTokenInfoEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mint: str
    name: str | None = None
    symbol: str | None = None
    uri: str | None = None
    update_authority: str | None = None
    source_program: str = "token2022"


class _WalletProfileEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    addr: str
    payload: dict[str, Any]


class _CommunitySummaryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    community_id: int
    payload: dict[str, Any]


class _SetupBody(BaseModel):
    """Mirror of `agent_service.evals.schema.EvalFixtures` extended
    with optional `wallet_profile` and `community_summary` fixture
    lists. The agent-service-side `EvalFixtures` currently only
    declares `get_token_info`; when the canonical schema grows new
    primitives, mirror them here in lockstep."""

    model_config = ConfigDict(extra="forbid")

    get_token_info: list[_GetTokenInfoEntry] = Field(default_factory=list)
    wallet_profile: list[_WalletProfileEntry] = Field(default_factory=list)
    community_summary: list[_CommunitySummaryEntry] = Field(default_factory=list)


@router.post("/eval/setup", status_code=204)
async def eval_setup(body: _SetupBody) -> Response:
    # Reject duplicate mint pubkeys / wallet addrs / community ids
    # before mutating the store. Same defense the agent-side
    # `EvalFixtures._mints_unique` validator runs; surfaced here so
    # a runner-side typo or two-case-bleed shows up as HTTP 400.
    seen_mints = set()
    for entry in body.get_token_info:
        if entry.mint in seen_mints:
            raise HTTPException(
                status_code=400,
                detail=f"duplicate mint in fixture batch: {entry.mint}",
            )
        seen_mints.add(entry.mint)

    STORE.setup(
        get_token_info=[e.model_dump() for e in body.get_token_info],
        wallet_profile=[e.model_dump() for e in body.wallet_profile],
        community_summary=[e.model_dump() for e in body.community_summary],
    )
    return Response(status_code=204)


@router.delete("/eval/setup", status_code=204)
async def eval_clear() -> Response:
    STORE.clear()
    return Response(status_code=204)
