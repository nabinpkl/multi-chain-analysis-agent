"""Ship 5a `${ref:N}` placeholder parser + index validator.

Single deterministic check the gate runs on Claim body_markdown and
Narrative text: every `${ref:N}` token must point at a valid index in
the surrounding provenance array. Out-of-bounds means retract.

Direct port of `backend/src/agent/policy_placeholder.rs`. Same regex
grammar, same first-error semantics. Unicode safe: regex match
positions are always char-boundary, captured digits are ASCII.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

# `\$\{ref:(\d+)\}`. Captures the index as group 1. Anchored only to the
# literal characters; surrounding prose ignored.
_REF_RE = re.compile(r"\$\{ref:(\d+)\}")


@dataclass(frozen=True, slots=True)
class RefError:
    """Reason a placeholder failed validation. The retract message
    surfaces this on `NarrativeRetracted` / claim retraction so the
    model's retry-feedback message can name the specific failure.

    Two variants share the dataclass shape (lighter than a discriminated
    union for two cases that both carry the same fields). `kind` selects
    between "out_of_bounds" and "parse_fail"; `index` populates only for
    out-of-bounds, `raw` only for parse fail."""

    kind: str
    index: int = 0
    provenance_len: int = 0
    raw: str = ""

    def to_human_string(self) -> str:
        if self.kind == "out_of_bounds":
            entry_word = "entry" if self.provenance_len == 1 else "entries"
            return (
                f"${{ref:{self.index}}} is out of bounds; "
                f"provenance has {self.provenance_len} {entry_word}"
            )
        if self.kind == "parse_fail":
            return f"could not parse `${{ref:{self.raw}}}` as a u32 index"
        return f"unknown placeholder error: {self.kind}"


def validate_refs(text: str, provenance_len: int) -> RefError | None:
    """Walk `text` for every `${ref:N}` token, parse N, verify it's a
    valid index into the surrounding provenance array. Returns the first
    error encountered, or `None` if every ref resolves (or the text
    contains no refs at all).

    First-error semantics matches the Rust gate: model sees one retract
    reason per turn, self-corrects on retry, subsequent bad refs surface
    on the next attempt if still wrong."""
    for match in _REF_RE.finditer(text):
        raw = match.group(1)
        try:
            n = int(raw)
        except ValueError:
            return RefError(kind="parse_fail", raw=raw)
        # u32 ceiling: defensive but realistic provenance arrays never approach it.
        if n < 0 or n > 0xFFFFFFFF:
            return RefError(kind="parse_fail", raw=raw)
        if n >= provenance_len:
            return RefError(
                kind="out_of_bounds", index=n, provenance_len=provenance_len
            )
    return None


def count_refs(text: str) -> int:
    """Count placeholder tokens without validating indices. Useful for
    path-trace notes and the structural gate's "no refs at all" branch."""
    return sum(1 for _ in _REF_RE.finditer(text))


def iter_ref_indices(text: str) -> Iterator[int | RefError]:
    """Iterator over every parsed ref index in document order. Yields a
    `RefError` (parse_fail variant) on the rare digit-overflow case;
    callers can short-circuit on the first error if they want to."""
    for match in _REF_RE.finditer(text):
        raw = match.group(1)
        try:
            n = int(raw)
        except ValueError:
            yield RefError(kind="parse_fail", raw=raw)
            return
        if n < 0 or n > 0xFFFFFFFF:
            yield RefError(kind="parse_fail", raw=raw)
            return
        yield n
