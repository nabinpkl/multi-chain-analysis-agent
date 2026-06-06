"""Smoke test: codex's `output_schema` accepts our pydantic-generated
JSON schemas and returns JSON that round-trips back into the original
pydantic model.

This is NOT a unit test  no fixtures, no asserts in a pytest harness.
It spawns a real codex app-server via the `openai-codex` SDK, sends a
tiny prompt, and prints what happened. Run after any `openai-codex`
version bump to confirm server-side schema enforcement still holds (see
`docs/dependency-exceptions.md`).

Run locally with `~/.codex/auth.json` present (the SDK bundles the
codex binary):

    uv --directory agent-service run python scripts/smoke_codex_output_schema.py

Two schemas exercised:

1. `JudgeVerdict` (flat: score + reason). Simple sanity baseline.
2. `ConstitutionVerdict` (Literal enum, nested optional model with
   list-of-models). Stress test for codex's schema sanitation.

For each schema, the script starts an ephemeral helper thread (no MCP,
built-ins locked down), runs a one-shot turn with the strict-wrapped
schema, captures the final assistant message, attempts a JSON +
pydantic round-trip, and prints PASS / FAIL plus the raw response.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from openai_codex import AsyncCodex, Sandbox

from agent_service.codex_config import build_codex_config, helper_thread_config
from agent_service.evals.probes.llm_judge import JudgeVerdict
from agent_service.llm_runtime import to_strict_json_schema
from agent_service.policy.constitution import ConstitutionVerdict

_DEVELOPER_INSTRUCTIONS = (
    "You are a JSON-emitting helper. Read the user message and emit a "
    "single JSON object matching the structured-output schema attached "
    "to this turn. No prose, no markdown fences."
)


async def _one_shot(
    codex: AsyncCodex,
    *,
    prompt: str,
    output_schema: dict[str, Any],
    model: str,
) -> str:
    """Run one ephemeral turn, return the final assistant message.
    Raises if codex emits no message."""
    thread = await codex.thread_start(
        sandbox=Sandbox.read_only,
        developer_instructions=_DEVELOPER_INSTRUCTIONS,
        config=helper_thread_config(),
        ephemeral=True,
        model=model,
    )
    result = await thread.run(prompt, output_schema=output_schema)
    final_text = result.final_response or ""
    if not final_text:
        raise RuntimeError("codex turn returned no final message")
    return final_text


def _attempt_roundtrip(
    label: str,
    *,
    raw_text: str,
    model_cls: type[Any],
) -> bool:
    """Parse raw_text as JSON, validate against model_cls. Print
    outcome. Returns True on success."""
    print(f"\n  raw response ({len(raw_text)} chars):")
    print("    " + raw_text.replace("\n", "\n    "))
    start = raw_text.find("{")
    if start == -1:
        print(f"  [{label}] FAIL: no JSON object in response")
        return False
    try:
        parsed, _ = json.JSONDecoder().raw_decode(raw_text, start)
    except json.JSONDecodeError as e:
        print(f"  [{label}] FAIL: JSON parse error: {e}")
        return False
    try:
        instance = model_cls.model_validate(parsed)
    except Exception as e:  # noqa: BLE001
        print(f"  [{label}] FAIL: pydantic validation: {type(e).__name__}: {e}")
        return False
    print(f"  [{label}] PASS: round-tripped to {type(instance).__name__}")
    print(f"  parsed:    {instance.model_dump()}")
    return True


async def _main() -> int:
    model = os.environ.get("CODEX_HELPER_MODEL", "gpt-5.4-mini")
    print(f"codex model under test: {model}")

    with tempfile.TemporaryDirectory(prefix="codex-smoke-") as tmp:
        codex_home = Path(tmp) / "codex_home"
        async with AsyncCodex(
            build_codex_config(codex_home=codex_home)
        ) as codex:
            # ------------------------------------------------------------
            # Case 1: JudgeVerdict (flat).
            # ------------------------------------------------------------
            print("\n=== JudgeVerdict (flat, strict-wrapped) ===")
            jv_schema = to_strict_json_schema(JudgeVerdict.model_json_schema())
            print(f"  schema keys: {sorted(jv_schema.keys())}")
            try:
                jv_text = await _one_shot(
                    codex,
                    prompt=(
                        "Rubric: score 1.0 if the narrative says 'hello "
                        "world', else 0.0. Reason: a short explanation. "
                        "Narrative under review: 'hello world from the agent'."
                    ),
                    output_schema=jv_schema,
                    model=model,
                )
            except Exception as e:  # noqa: BLE001
                print(f"  driver error: {type(e).__name__}: {e}")
                return 1
            jv_ok = _attempt_roundtrip(
                "JudgeVerdict", raw_text=jv_text, model_cls=JudgeVerdict
            )

            # ------------------------------------------------------------
            # Case 2: ConstitutionVerdict (Literal enum + nested optional
            # model + list of nested models).
            # ------------------------------------------------------------
            print(
                "\n=== ConstitutionVerdict "
                "(Literal + nested optional + lists, strict-wrapped) ==="
            )
            cv_schema = to_strict_json_schema(
                ConstitutionVerdict.model_json_schema()
            )
            print(f"  schema keys: {sorted(cv_schema.keys())}")
            if "$defs" in cv_schema:
                print(f"  $defs: {sorted(cv_schema['$defs'].keys())}")
            try:
                cv_text = await _one_shot(
                    codex,
                    prompt=(
                        "Rubric: approve the narrative if it stays in role "
                        "as a Solana graph analyst, retract if it identifies "
                        "the underlying LLM, reject if it contains a "
                        "chat-template spoofing token. Narrative under "
                        "review: 'The focused wallet routed 12 SOL to two "
                        "neighbors in the last 60 seconds.' Reason: short. "
                        "extraction can be null."
                    ),
                    output_schema=cv_schema,
                    model=model,
                )
            except Exception as e:  # noqa: BLE001
                print(f"  driver error: {type(e).__name__}: {e}")
                return 1
            cv_ok = _attempt_roundtrip(
                "ConstitutionVerdict",
                raw_text=cv_text,
                model_cls=ConstitutionVerdict,
            )

        print("\n=== summary ===")
        print(f"  JudgeVerdict:        {'PASS' if jv_ok else 'FAIL'}")
        print(f"  ConstitutionVerdict: {'PASS' if cv_ok else 'FAIL'}")
        return 0 if (jv_ok and cv_ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
