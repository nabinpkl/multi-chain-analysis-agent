"""Phase I.5 stub: assert the Rust SSE golden files exist and parse
as valid SSE event blocks. Phase II onward uses the same goldens as
byte-diff oracles when the Python service emits frames; we lock the
parse path here so a malformed golden surfaces immediately.

A golden file is a sequence of `event: <name>\\ndata: <json>\\n\\n`
blocks (one per emitted frame in a turn). Every `data:` line must be
JSON-parseable, and every block must round-trip through the matching
pydantic model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_service.wire.agent import (
    AgentDone,
    ChangedSince,
    Claim,
    Error,
    GatePath,
    NarrativeRetracted,
    NarrativeWithRefs,
    NoMovement,
    Progress,
)


_GOLDENS_DIR = Path(__file__).resolve().parents[1] / "goldens"


# Minimum scenario set captured by `cargo run --bin dump_sse_goldens`.
# If a scenario gets removed, the assertion fails and we know to
# update either the dumper or this list.
EXPECTED_SCENARIOS = [
    "happy_path_wallet_profile",
    "happy_path_emit_claim_with_narrative",
    "narrative_retracted_by_constitution",
    "no_movement",
    "changed_since",
    "error_terminal",
    "gate_path_show_trace",
]

# Map event-name → matching pydantic model. `Done` (AgentDone) is
# the closer; everything else is an SseFrame variant.
EVENT_TO_MODEL = {
    "Claim": Claim,
    "Progress": Progress,
    "Narrative": NarrativeWithRefs,
    "NarrativeRetracted": NarrativeRetracted,
    "Error": Error,
    "GatePath": GatePath,
    "NoMovement": NoMovement,
    "ChangedSince": ChangedSince,
    "Done": AgentDone,
}


def _parse_sse_blocks(text: str) -> list[tuple[str, str]]:
    """Split an SSE byte stream into [(event_name, data_json), ...].
    Trailing blank line per spec; we tolerate either CRLF or LF."""
    blocks: list[tuple[str, str]] = []
    current_event: str | None = None
    current_data: list[str] = []
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if line.startswith("event: "):
            current_event = line[len("event: "):]
        elif line.startswith("data: "):
            current_data.append(line[len("data: "):])
        elif line == "":
            if current_event is not None and current_data:
                blocks.append((current_event, "\n".join(current_data)))
            current_event = None
            current_data = []
        else:
            # Comments or stray lines: SSE allows but we don't expect any.
            pass
    return blocks


def test_goldens_dir_exists():
    assert _GOLDENS_DIR.is_dir(), (
        f"goldens dir missing: {_GOLDENS_DIR}. "
        f"Run `just regen-sse-goldens`."
    )


@pytest.mark.parametrize("scenario", EXPECTED_SCENARIOS)
def test_golden_file_exists(scenario: str):
    """Every expected scenario has a `.sse` file."""
    path = _GOLDENS_DIR / f"{scenario}.sse"
    assert path.is_file(), (
        f"missing golden: {path}. Run `just regen-sse-goldens`."
    )


@pytest.mark.parametrize("scenario", EXPECTED_SCENARIOS)
def test_golden_parses_as_sse(scenario: str):
    """Each block is well-formed: known event name + JSON-parseable
    data."""
    body = (_GOLDENS_DIR / f"{scenario}.sse").read_text(encoding="utf-8")
    blocks = _parse_sse_blocks(body)
    assert blocks, f"{scenario}: no SSE blocks parsed"
    for event, data in blocks:
        assert event in EVENT_TO_MODEL, (
            f"{scenario}: unknown event name `{event}`"
        )
        # JSON must parse.
        json.loads(data)


@pytest.mark.parametrize("scenario", EXPECTED_SCENARIOS)
def test_golden_round_trips_through_pydantic(scenario: str):
    """Every payload validates against the matching pydantic model.
    This is the actual parity oracle: if Rust emits a shape Python
    can't parse, this fails. Phase II's loop emits the same shapes
    via these models, so byte-equivalent output is guaranteed."""
    body = (_GOLDENS_DIR / f"{scenario}.sse").read_text(encoding="utf-8")
    blocks = _parse_sse_blocks(body)
    for event, data in blocks:
        model_cls = EVENT_TO_MODEL[event]
        try:
            instance = model_cls.model_validate_json(data)
        except Exception as e:  # noqa: BLE001
            pytest.fail(
                f"{scenario}: event `{event}` failed pydantic validation:\n"
                f"  data: {data}\n"
                f"  error: {e!r}"
            )
        # Round-trip: dump and re-parse to confirm symmetry.
        instance2 = model_cls.model_validate_json(instance.model_dump_json())
        assert instance.model_dump(mode="json") == instance2.model_dump(mode="json")


def test_every_event_type_appears_at_least_once():
    """Inventory check: across all scenarios, every SSE event we
    expect to support has at least one occurrence in the goldens.
    If a frame variant isn't represented, Phase II loses its byte
    oracle for that variant."""
    seen: set[str] = set()
    for scenario in EXPECTED_SCENARIOS:
        body = (_GOLDENS_DIR / f"{scenario}.sse").read_text(encoding="utf-8")
        for event, _ in _parse_sse_blocks(body):
            seen.add(event)
    missing = set(EVENT_TO_MODEL.keys()) - seen
    assert not missing, (
        f"event types not exercised by any golden: {sorted(missing)}. "
        f"Add a scenario to dump_sse_goldens.rs."
    )
