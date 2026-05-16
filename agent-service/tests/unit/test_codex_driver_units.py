"""Unit coverage for codex-driver MCP wrapper parsing and binding
population.

Two functions in `agent_service.codex_driver` carry the
"binding-store-from-codex-MCP-output" path that lets the structural
value-compare gate (in `core.post_tools.run_post_tools_phase`) run
over codex-emitted claims:

* `_extract_mcp_envelope`  parses the codex-cli MCP wrapper
  (`{"content":..., "structuredContent": {value, provenance}, ...}`)
  and returns the inner envelope. Codex's wrapper shape is undocumented
  across cli versions, so the parser stays defensive: any shape
  mismatch returns None.

* `_record_tool_output_binding`  the wiring between TOOL_COMPLETED
  events and `PrimitiveBindingStore.record(...)`. Skips
  `get_token_info` (no envelope) and tools that error (None envelope).

The structural gate itself is exercised through
`core.post_tools.run_post_tools_phase` and the runtime-parity
hermetic eval; codex no longer holds its own copy of the gate stack
since the alignment work.

No codex subprocess, no SSE, no docker. All inputs are constructed in
Python so each test exercises one parser branch.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agent_service.codex_driver import (
    _capped_json,
    _digest12,
    _extract_mcp_envelope,
    _read_codex_model,
    _record_tool_output_binding,
)
from agent_service.thread_state import AgentThread


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_thread(thread_id: str = "t1") -> AgentThread:
    """Bare thread with an empty binding store. Caller populates it
    via `_record_tool_output_binding` to set up structural-gate
    fixtures."""
    return AgentThread(thread_id=thread_id, started_at_ms=0)


def _wallet_profile_output_json(
    addr: str = "fueL3hBZjLLLJHiFH9cqZoozTG3XQZ53diwFPwbzNim",
    degree: int = 39,
    community_id: int = 519,
) -> str:
    """One realistic `TOOL_COMPLETED.output` payload for
    `wallet_profile`. Codex wraps an MCP tool's `result` as
    `{"content": [...], "structuredContent": <our envelope>, ...}`
    and `_tool_output(item)` json-dumps the whole thing. The
    envelope itself is the chunk-3.5 `{value, provenance}` shape
    the Rust backend serializes from `McpEnvelope`."""
    envelope = {
        "value": {
            "addr": addr,
            "community_id": community_id,
            "stats": {
                "degree": degree,
                "spl_degree": degree,
                "sol_degree": 0,
            },
        },
        "provenance": [
            {"kind": "wallet", "addr": addr, "idx": 1026},
            {"kind": "community", "id": community_id},
            {
                "kind": "number",
                "metric": "degree",
                "value": float(degree),
                "support": [addr],
            },
        ],
    }
    return json.dumps(
        {
            "content": [{"type": "text", "text": "wallet profile result"}],
            "structuredContent": envelope,
            "_meta": None,
        }
    )


# ---------------------------------------------------------------------------
# _extract_mcp_envelope
# ---------------------------------------------------------------------------


def test_extract_envelope_happy_path():
    """Well-formed MCP wrapper → `(value, provenance)` tuple."""
    out = _extract_mcp_envelope(_wallet_profile_output_json())
    assert out is not None
    value, provenance = out
    assert value["addr"].startswith("fueL3")
    assert value["stats"]["degree"] == 39
    assert isinstance(provenance, list)
    assert len(provenance) == 3
    assert provenance[0]["kind"] == "wallet"


def test_extract_envelope_none_when_output_empty():
    """`None`/empty string is the codex-cli no-output shape (e.g.
    cancelled mid-stream). The parser short-circuits before json
    parsing."""
    assert _extract_mcp_envelope(None) is None
    assert _extract_mcp_envelope("") is None


def test_extract_envelope_none_on_invalid_json():
    """Bad JSON from codex (rare; usually means cli upgrade broke the
    wrapper shape) returns None rather than raising. Callers no-op
    on None so a single malformed tool call doesn't fail the turn."""
    assert _extract_mcp_envelope("not-json-at-all") is None


def test_extract_envelope_none_on_missing_structured_content():
    """codex sometimes lands here for tool errors  `result` is the
    error message dict, not a wrapper around `structuredContent`."""
    payload = json.dumps({"content": [{"type": "text", "text": "boom"}]})
    assert _extract_mcp_envelope(payload) is None


def test_extract_envelope_none_when_value_not_object():
    """`get_token_info` returns a flat string/dict shape; if codex
    routes that through `structuredContent` directly the value
    isn't a dict and the parser bails. Keeps the binding-store
    population sterile to the two graph primitives that actually
    carry a `{value, provenance}` envelope today."""
    payload = json.dumps(
        {"structuredContent": {"value": "not-a-dict", "provenance": []}}
    )
    assert _extract_mcp_envelope(payload) is None


def test_extract_envelope_none_when_provenance_not_list():
    """Schema drift guard: if `provenance` arrives as something
    other than a JSON array, drop the whole envelope rather than
    feeding garbage into `_provenance_refs_from_json`."""
    payload = json.dumps(
        {
            "structuredContent": {
                "value": {"addr": "X"},
                "provenance": "should-be-a-list",
            }
        }
    )
    assert _extract_mcp_envelope(payload) is None


def test_extract_envelope_accepts_empty_provenance():
    """Empty provenance is legal at the envelope layer (the
    placeholder gate enforces non-empty per-claim later). Parser
    returns the empty list rather than treating it as a shape
    error."""
    payload = json.dumps(
        {"structuredContent": {"value": {"addr": "X"}, "provenance": []}}
    )
    out = _extract_mcp_envelope(payload)
    assert out is not None
    _, provenance = out
    assert provenance == []


# ---------------------------------------------------------------------------
# _record_tool_output_binding
# ---------------------------------------------------------------------------


def test_record_binding_populates_store_for_wallet_profile():
    """End-to-end: a TOOL_COMPLETED.output for `wallet_profile`
    lands in `thread.bindings` with the wallet, community, and
    extracted numbers walked out of `value` + provenance refs."""
    thread = _make_thread()
    assert thread.bindings.is_empty()
    _record_tool_output_binding(
        thread=thread,
        tool_name="wallet_profile",
        output_json=_wallet_profile_output_json(degree=39),
    )
    assert not thread.bindings.is_empty()
    # The store walks both `value` numbers and provenance Number
    # refs; `degree=39` appears in both, so the binding's numbers
    # list carries it (possibly twice  the gate is value-compare,
    # not de-dup-aware).
    nums = [n.value for n in thread.bindings.all_numbers()]
    assert 39.0 in nums
    wallets = thread.bindings.all_wallets()
    assert "fueL3hBZjLLLJHiFH9cqZoozTG3XQZ53diwFPwbzNim" in wallets
    assert 519 in thread.bindings.all_communities()


def test_record_binding_skips_get_token_info():
    """`get_token_info` returns a bare value (no `{value,
    provenance}` envelope) so binding population is a no-op. The
    structural gate will then see no entries for that tool's
    output, same fallback as the pydantic-ai path's behavior on
    non-binding primitives."""
    thread = _make_thread()
    _record_tool_output_binding(
        thread=thread,
        tool_name="get_token_info",
        output_json='{"structuredContent": {"value": {"a": 1}, "provenance": []}}',
    )
    assert thread.bindings.is_empty()


def test_record_binding_skips_when_envelope_malformed():
    """Tool error / schema drift → `_extract_mcp_envelope` returns
    None → recorder is a no-op. The turn proceeds; the structural
    gate just won't have anything to verify for this tool call."""
    thread = _make_thread()
    _record_tool_output_binding(
        thread=thread,
        tool_name="wallet_profile",
        output_json="not-json",
    )
    assert thread.bindings.is_empty()


def test_record_binding_skips_when_output_none():
    """Codex emits TOOL_COMPLETED with output=None when a tool
    call was cancelled mid-stream. Recorder bails before parsing."""
    thread = _make_thread()
    _record_tool_output_binding(
        thread=thread,
        tool_name="wallet_profile",
        output_json=None,
    )
    assert thread.bindings.is_empty()



# ---------------------------------------------------------------------------
# _capped_json + _digest12
# ---------------------------------------------------------------------------


def test_capped_json_passthrough_under_cap():
    """Small payload survives serialization intact; round-trip
    matches the source dict."""
    out = _capped_json({"a": 1, "b": [2, 3]})
    assert json.loads(out) == {"a": 1, "b": [2, 3]}


def test_capped_json_truncates_with_marker():
    """Payload over the cap is truncated and the marker names the
    original total byte count so SQL filters can identify caps."""
    big = "x" * 16000
    out = _capped_json({"k": big}, cap=8192)
    assert "[truncated, total=" in out
    assert len(out.encode("utf-8")) <= 8192 + 64  # cap + marker slack


def test_capped_json_handles_none():
    """`None` short-circuits to empty string  no JSON serialization
    attempted. Lets callers stamp PRIMITIVE_OUTPUT unconditionally
    even when codex sends back null output."""
    assert _capped_json(None) == ""


def test_capped_json_swallows_non_serializable():
    """Unserializable input (e.g. a raw Python object) collapses to
    empty string rather than raising; one bad tool call shouldn't
    break the span."""

    class NotJsonable:
        pass

    # default=str converts unknown types to their repr, so empty
    # only when json.dumps raises despite that fallback. Force a
    # value that breaks default=str too via circular ref.
    a = {}
    a["self"] = a
    assert _capped_json(a) == ""


def test_digest12_stable_across_calls():
    """Same input -> same digest. Lets eval probes compare digests
    across runtimes (pydantic-ai's primitive_client stamps the same
    `_digest12` shape on `mcae.primitive.output_digest`)."""
    s = '{"value": {"degree": 39}, "provenance": []}'
    assert _digest12(s) == _digest12(s)
    assert len(_digest12(s)) == 12


def test_digest12_handles_none_and_empty():
    """`None` -> empty string; empty string -> well-defined sha256
    prefix. Both paths must not raise."""
    assert _digest12(None) == ""
    assert len(_digest12("")) == 12


# ---------------------------------------------------------------------------
# _read_codex_model
# ---------------------------------------------------------------------------


def _seed_codex_sqlite(
    codex_home_root: Path,
    thread_id: str,
    provider_thread_id: str,
    model: str,
) -> Path:
    """Create a `state_5.sqlite` at the canonical codex path with
    one `threads` row. Matches the schema codex-cli writes on
    thread start (only the columns the helper touches are
    populated; the helper uses `SELECT model FROM threads WHERE
    id = ?` which only needs `id` + `model`)."""
    db_dir = codex_home_root / "local" / thread_id / "sqlite"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "state_5.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model TEXT)"
        )
        conn.execute(
            "INSERT INTO threads (id, model) VALUES (?, ?)",
            (provider_thread_id, model),
        )
        conn.commit()
    return db_path


def test_read_codex_model_happy_path(tmp_path: Path):
    """A populated sqlite at the expected path returns the model
    string we'd stamp as `gen_ai.request.model`."""
    _seed_codex_sqlite(
        tmp_path, "thread-1", "provider-thread-1", "gpt-5.5"
    )
    assert (
        _read_codex_model(
            codex_home_root=tmp_path,
            thread_id="thread-1",
            provider_thread_id="provider-thread-1",
        )
        == "gpt-5.5"
    )


def test_read_codex_model_none_when_codex_home_missing(tmp_path: Path):
    """`codex_home_root=None` short-circuits before any I/O. Covers
    the test/dev environments where the codex runtime isn't
    available (LoopHandles.codex_home_root stays None)."""
    assert (
        _read_codex_model(
            codex_home_root=None,
            thread_id="thread-1",
            provider_thread_id="provider-thread-1",
        )
        is None
    )


def test_read_codex_model_none_when_provider_thread_id_empty(tmp_path: Path):
    """First turn on a thread emits `provider_thread_id_local=""`
    until codex stamps one. We short-circuit so we don't run a
    `WHERE id = ""` query that would never match."""
    _seed_codex_sqlite(
        tmp_path, "thread-1", "provider-thread-1", "gpt-5.5"
    )
    assert (
        _read_codex_model(
            codex_home_root=tmp_path,
            thread_id="thread-1",
            provider_thread_id="",
        )
        is None
    )


def test_read_codex_model_none_when_db_path_missing(tmp_path: Path):
    """Thread directory exists but sqlite never landed (rare;
    happens if a turn was cancelled before codex flushed). Helper
    returns None rather than raising."""
    # Create thread dir without state_5.sqlite
    (tmp_path / "local" / "thread-1" / "sqlite").mkdir(parents=True)
    assert (
        _read_codex_model(
            codex_home_root=tmp_path,
            thread_id="thread-1",
            provider_thread_id="provider-thread-1",
        )
        is None
    )


def test_read_codex_model_none_when_row_absent(tmp_path: Path):
    """sqlite exists but the provider_thread_id we're looking up
    isn't there (rare; would mean codex stamped a different id
    than the one we recovered from the stream). Helper returns
    None, caller skips the model stamp."""
    _seed_codex_sqlite(
        tmp_path, "thread-1", "provider-thread-1", "gpt-5.5"
    )
    assert (
        _read_codex_model(
            codex_home_root=tmp_path,
            thread_id="thread-1",
            provider_thread_id="some-other-id",
        )
        is None
    )


