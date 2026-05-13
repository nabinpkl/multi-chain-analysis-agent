"""Unit coverage for the chunk 3.5 codex-driver helpers that are
otherwise smoke-tested only.

Three functions in `agent_service.codex_driver` carry the
"binding-store-from-codex-MCP-output" path that lets the structural
value-compare gate run over codex-emitted claims:

* `_extract_mcp_envelope`  parses the codex-cli MCP wrapper
  (`{"content":..., "structuredContent": {value, provenance}, ...}`)
  and returns the inner envelope. Codex's wrapper shape is undocumented
  across cli versions, so the parser stays defensive: any shape
  mismatch returns None.

* `_record_tool_output_binding`  the wiring between TOOL_COMPLETED
  events and `PrimitiveBindingStore.record(...)`. Skips
  `get_token_info` (no envelope) and tools that error (None envelope).

* The structural-gate retract path in `_emit_claims_from_drain`
  the chunk 3.5 item 6 payoff. With a populated binding store and
  `dont_fabricate=True`, a claim whose support number doesn't trace
  to any prior tool output must be retracted with a
  number_not_in_binding mismatch.

No codex subprocess, no SSE, no docker. All inputs are constructed in
Python so each test exercises one parser branch.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from agent_service.codex_driver import (
    _capped_json,
    _digest12,
    _emit_claims_from_drain,
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
# Structural gate retract path through _emit_claims_from_drain
# ---------------------------------------------------------------------------


def test_drain_retracts_claim_with_unsourced_number_when_dont_fabricate():
    """The chunk 3.5 item 6 payoff. Sequence:

    1. A codex tool output lands in the binding store via
       `_record_tool_output_binding` (degree=39).
    2. A claim arrives in the drain with provenance citing
       `metric=degree, value=900`  900 is way outside the 10%
       tolerance band around 39, so the structural gate flags it.
    3. With `dont_fabricate=True`, the gate retracts; the claim
       comes back from `_emit_claims_from_drain` with `approved=False`
       and a `RETRACTED` policy verdict carrying the gate reason.

    Mirrors the pydantic-ai path's behavior in `core/run.py` so
    the two runtimes stay observably equivalent under the same
    gate semantics."""
    thread = _make_thread()
    _record_tool_output_binding(
        thread=thread,
        tool_name="wallet_profile",
        output_json=_wallet_profile_output_json(degree=39),
    )
    # Claim cites an out-of-band number (900 vs binding's 39).
    drained = [
        {
            "kind": "profile",
            "headline": "Wallet has huge degree",
            "body_markdown": "Wallet ${ref:0} has degree `900`.",
            "provenance": [
                {
                    "kind": "wallet",
                    "addr": "fueL3hBZjLLLJHiFH9cqZoozTG3XQZ53diwFPwbzNim",
                },
                {"kind": "number", "metric": "degree", "value": 900.0},
            ],
        }
    ]
    results = _emit_claims_from_drain(
        drained=drained,
        thread=thread,
        thread_id="t1",
        turn_started_at_ms=0,
        dont_fabricate=True,
    )
    assert len(results) == 1
    claim, approved = results[0]
    assert approved is False
    # Verdict carries the gate reason inline so the UI can render
    # the retraction without a second roundtrip.
    verdict_case = claim.policy_verdict.WhichOneof("verdict")
    assert verdict_case == "retracted"
    assert "degree" in claim.policy_verdict.retracted.reason


def test_drain_approves_same_claim_when_dont_fabricate_off():
    """Same fixture as above but `dont_fabricate=False`. The gate
    still RUNS (and a span gets stamped), but the codex path's
    "observe-without-acting" branch lets the claim through. This
    lets the ablation suite compare gated vs ungated codex output
    on the same prompt."""
    thread = _make_thread()
    _record_tool_output_binding(
        thread=thread,
        tool_name="wallet_profile",
        output_json=_wallet_profile_output_json(degree=39),
    )
    drained = [
        {
            "kind": "profile",
            "headline": "Wallet has huge degree",
            "body_markdown": "Wallet ${ref:0} has degree `900`.",
            "provenance": [
                {
                    "kind": "wallet",
                    "addr": "fueL3hBZjLLLJHiFH9cqZoozTG3XQZ53diwFPwbzNim",
                },
                {"kind": "number", "metric": "degree", "value": 900.0},
            ],
        }
    ]
    results = _emit_claims_from_drain(
        drained=drained,
        thread=thread,
        thread_id="t1",
        turn_started_at_ms=0,
        dont_fabricate=False,
    )
    assert len(results) == 1
    _claim, approved = results[0]
    assert approved is True


def test_drain_approves_claim_whose_numbers_trace_to_binding():
    """Sanity opposite: when the claim's cited number matches the
    binding (within tolerance), the structural gate approves even
    with `dont_fabricate=True`."""
    thread = _make_thread()
    _record_tool_output_binding(
        thread=thread,
        tool_name="wallet_profile",
        output_json=_wallet_profile_output_json(degree=39),
    )
    drained = [
        {
            "kind": "profile",
            "headline": "Wallet's degree is 39",
            "body_markdown": "Wallet ${ref:0} has degree `39`.",
            "provenance": [
                {
                    "kind": "wallet",
                    "addr": "fueL3hBZjLLLJHiFH9cqZoozTG3XQZ53diwFPwbzNim",
                },
                {"kind": "number", "metric": "degree", "value": 39.0},
            ],
        }
    ]
    results = _emit_claims_from_drain(
        drained=drained,
        thread=thread,
        thread_id="t1",
        turn_started_at_ms=0,
        dont_fabricate=True,
    )
    assert len(results) == 1
    claim, approved = results[0]
    assert approved is True
    assert claim.policy_verdict.WhichOneof("verdict") == "approved"


def test_drain_retracts_claim_with_empty_provenance_regardless_of_switch():
    """Even with `dont_fabricate=False`, the empty-provenance
    short-circuit fires (chunk 3.5 keeps the placeholder gate as a
    hard floor). The structural gate's observe-without-acting
    branch only applies AFTER provenance + ${ref:N} checks pass."""
    thread = _make_thread()
    drained = [
        {
            "kind": "profile",
            "headline": "No citations",
            "body_markdown": "Mystery claim with no refs.",
            "provenance": [],
        }
    ]
    results = _emit_claims_from_drain(
        drained=drained,
        thread=thread,
        thread_id="t1",
        turn_started_at_ms=0,
        dont_fabricate=False,
    )
    assert len(results) == 1
    claim, approved = results[0]
    assert approved is False
    assert (
        "provenance" in claim.policy_verdict.retracted.reason.lower()
    )


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


# ---------------------------------------------------------------------------
# mcae.claim.emitted span coverage
# ---------------------------------------------------------------------------


@pytest.fixture
def span_exporter():
    """Swap the codex_driver module's `_tracer` for one backed by
    an in-memory exporter so we can assert span emission shape.

    Two concessions to the test harness's environment:

    1. OTel's global TracerProvider can only be set once per
       process (later sets warn + are ignored), so we don't touch
       the global. We build a fresh local `TracerProvider`, get a
       tracer from it, and monkey-patch the module-level `_tracer`
       reference codex_driver captured at import time.

    2. `tests/conftest.py` sets `OTEL_SDK_DISABLED=true` to keep
       the FastAPI lifespan's `init_otel` from trying to reach
       the in-compose `otel-collector` hostname. With that flag,
       OTel SDK constructors short-circuit to no-op tracers
       regardless of how the provider was built. We temporarily
       unset the flag for the fixture's lifetime so our local
       provider stays live.

    Both effects are reverted on teardown."""
    import os

    prior_disabled = os.environ.pop("OTEL_SDK_DISABLED", None)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local_tracer = provider.get_tracer("agent_service.codex_driver")

    import agent_service.codex_driver as cd

    original_tracer = cd._tracer
    cd._tracer = local_tracer
    try:
        yield exporter
    finally:
        cd._tracer = original_tracer
        if prior_disabled is not None:
            os.environ["OTEL_SDK_DISABLED"] = prior_disabled


def _spans_by_name(
    exporter: InMemorySpanExporter, name: str
) -> list:
    return [s for s in exporter.get_finished_spans() if s.name == name]


def test_drain_emits_claim_emitted_span_with_primitive_source_kind(
    span_exporter: InMemorySpanExporter,
):
    """Phase-2: every drained claim now produces a
    `mcae.claim.emitted` span with `mcae.claim.source_kind=primitive`.
    Without this, the `claim_grounded_in(source_kind=primitive)`
    eval probe vacuously passed because no claim spans existed on
    the codex path."""
    thread = _make_thread()
    _record_tool_output_binding(
        thread=thread,
        tool_name="wallet_profile",
        output_json=_wallet_profile_output_json(degree=39),
    )
    drained = [
        {
            "kind": "profile",
            "headline": "Wallet has degree 39",
            "body_markdown": "Wallet ${ref:0} has degree `39`.",
            "provenance": [
                {
                    "kind": "wallet",
                    "addr": "fueL3hBZjLLLJHiFH9cqZoozTG3XQZ53diwFPwbzNim",
                },
                {"kind": "number", "metric": "degree", "value": 39.0},
            ],
        }
    ]
    _emit_claims_from_drain(
        drained=drained,
        thread=thread,
        thread_id="t1",
        turn_started_at_ms=0,
        dont_fabricate=True,
    )
    claim_spans = _spans_by_name(span_exporter, "mcae.claim.emitted")
    assert len(claim_spans) == 1
    attrs = dict(claim_spans[0].attributes or {})
    assert attrs.get("mcae.claim.source_kind") == "primitive"
    assert attrs.get("mcae.claim.kind") == "CLAIM_KIND_PROFILE"
    assert attrs.get("mcae.claim.provenance_count") == 2
    assert attrs.get("mcae.claim.verdict") == "approved"
    assert attrs.get("mcae.claim.headline") == "Wallet has degree 39"


def test_drain_claim_emitted_verdict_is_retracted_for_unsourced_number(
    span_exporter: InMemorySpanExporter,
):
    """When the structural gate retracts (number doesn't trace to
    binding + `dont_fabricate=True`), the claim_emitted span must
    carry `verdict=retracted` so the eval probe that asserts the
    claim's final outcome sees the correct value."""
    thread = _make_thread()
    _record_tool_output_binding(
        thread=thread,
        tool_name="wallet_profile",
        output_json=_wallet_profile_output_json(degree=39),
    )
    drained = [
        {
            "kind": "profile",
            "headline": "Fabricated number",
            "body_markdown": "Wallet ${ref:0} has degree `900`.",
            "provenance": [
                {
                    "kind": "wallet",
                    "addr": "fueL3hBZjLLLJHiFH9cqZoozTG3XQZ53diwFPwbzNim",
                },
                {"kind": "number", "metric": "degree", "value": 900.0},
            ],
        }
    ]
    _emit_claims_from_drain(
        drained=drained,
        thread=thread,
        thread_id="t1",
        turn_started_at_ms=0,
        dont_fabricate=True,
    )
    claim_spans = _spans_by_name(span_exporter, "mcae.claim.emitted")
    assert len(claim_spans) == 1
    attrs = dict(claim_spans[0].attributes or {})
    assert attrs.get("mcae.claim.verdict") == "retracted"


def test_drain_gate_spans_are_children_of_claim_emitted_span(
    span_exporter: InMemorySpanExporter,
):
    """Gate spans (`mcae.gate.placeholder`, `mcae.gate.structural`)
    must nest under `mcae.claim.emitted` so the trace tree groups
    every gate decision under the claim that triggered it. The
    pydantic-ai path emits this same shape; eval probes that walk
    the parent chain (or that count gates per claim) need codex
    parity."""
    thread = _make_thread()
    _record_tool_output_binding(
        thread=thread,
        tool_name="wallet_profile",
        output_json=_wallet_profile_output_json(degree=39),
    )
    drained = [
        {
            "kind": "profile",
            "headline": "h",
            "body_markdown": "Wallet ${ref:0} has degree `39`.",
            "provenance": [
                {
                    "kind": "wallet",
                    "addr": "fueL3hBZjLLLJHiFH9cqZoozTG3XQZ53diwFPwbzNim",
                },
                {"kind": "number", "metric": "degree", "value": 39.0},
            ],
        }
    ]
    _emit_claims_from_drain(
        drained=drained,
        thread=thread,
        thread_id="t1",
        turn_started_at_ms=0,
        dont_fabricate=True,
    )
    claim_spans = _spans_by_name(span_exporter, "mcae.claim.emitted")
    placeholder_spans = _spans_by_name(
        span_exporter, "mcae.gate.placeholder"
    )
    structural_spans = _spans_by_name(
        span_exporter, "mcae.gate.structural"
    )
    assert len(claim_spans) == 1
    assert len(placeholder_spans) == 1
    assert len(structural_spans) == 1
    claim_span_id = claim_spans[0].context.span_id
    assert placeholder_spans[0].parent.span_id == claim_span_id
    assert structural_spans[0].parent.span_id == claim_span_id


def test_drain_skips_claims_failing_pydantic_validation():
    """A drained payload missing `kind` (required by
    `EmitClaimInput.model_validate`) is dropped silently with a
    warning log; nothing comes back from the drain. The codex
    boundary already validates this on the Rust side (the chunk
    3.5 emit_claims aggregator catches missing kind), but the
    Python drain stays defensive so a schema drift doesn't
    poison the whole turn."""
    thread = _make_thread()
    drained = [
        {
            # No "kind"  pydantic will reject.
            "headline": "headline",
            "body_markdown": "body",
            "provenance": [{"kind": "wallet", "addr": "X"}],
        }
    ]
    results = _emit_claims_from_drain(
        drained=drained,
        thread=thread,
        thread_id="t1",
        turn_started_at_ms=0,
        dont_fabricate=True,
    )
    assert results == []
