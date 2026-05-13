"""System-prompt composer: parse `system_v4.txt`'s tagged-rule
structure, drop rules whose ids are in `drop_rule_ids`, return the
assembled prompt.

The .txt file is a hybrid XML+markdown document: the body wraps each
behavior rule in `<rule id="...">...</rule>` blocks inside a top-level
`<rules>` container. This shape is the 2026 industry-standard hybrid
recommended by Anthropic ("Use XML tags to structure your prompts",
living doc covering Claude Opus 4.7 / Sonnet 4.6) and used by the
canonical OpenAI GPT-5 prompting guide (2025-08-07). XML for major
sections + stable rule ids; markdown inside rules for in-section
structure.

Single source of truth: per project rule, we do NOT split prompts
into multiple files. Modularity comes from per-rule tags + this
composer, not from a directory of fragments.

Composer contract:
- Empty drop set returns the file verbatim. The production preset
  (every defense on) is observably equivalent to the pre-refactor
  flat file.
- Dropping a non-existent rule id raises CompositionError. A switch
  wired to a typo'd id ("defense:offdomain") would otherwise silently
  do nothing and the agent would report false defense success.
- A duplicate rule id in the source file raises CompositionError.
  An editor pasting a rule twice should fail loudly on load, not at
  the moment some reader's drop happens to remove only one of them.

Parser shape: regex over `<rule id="..."> ... </rule>` blocks.
Deliberately NOT an XML library because:
- The file is operator-controlled, not user input.
- Rules contain `<`, `>`, `&` in their markdown bodies (e.g. the
  literal token names `<|im_start|>` and `</user>` in the
  `defense:chat_template_rejection` rule). Escaping those would
  change what the model reads. Letting an XML parser handle them
  would either crash or normalize them; both are wrong.
- The structure we extract is shallow: one `<rules>` container, a
  flat list of `<rule>` children. Regex is the right tool.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from agent_service.prompts import load_prompt

if TYPE_CHECKING:
    from multichain.wire.agent.v1.switches_pb2 import AgentSwitches


class CompositionError(ValueError):
    """Raised on a malformed source file or an unknown drop id.

    Subclasses ValueError so existing broad-except sites at startup
    fail closed rather than silently composing an empty prompt.
    """


# Live-window placeholder substitution. Shared with
# `policy/constitution.py` so the primary prompt and the constitution
# prompt render the same window string for the same input  if they
# drift, the constitution gate retracts correct narratives for "wrong
# window" inconsistency. One enum-to-string table, one
# `substitute_live_window` helper, two callers.
#
# The placeholder is `${LIVE_WINDOW_HUMAN}` (literal dollar-sign +
# uppercase token) so it stands out in the prompt files and won't
# collide with any of the `<rule id="...">` machinery.
_LIVE_WINDOW_HUMAN: dict[int, str] = {
    10: "10 seconds",
    60: "60 seconds",
    300: "5 minutes",
    900: "15 minutes",
    1800: "30 minutes",
    3600: "1 hour",
}
_LIVE_WINDOW_PLACEHOLDER = "${LIVE_WINDOW_HUMAN}"

# Default window when no caller specifies one. Mirrors the Rust
# `TURN_BEGIN_DEFAULT_WINDOW_SECS` constant in
# `backend/src/api/primitives.rs` so the prompt's "last 60 seconds"
# matches the snapshot's 60s window for every caller that hasn't
# opted in to widen.
DEFAULT_LIVE_WINDOW_SECS = 60


def format_live_window(secs: int) -> str:
    """Render a window-seconds value as the human-readable string the
    prompt placeholder is replaced with.

    Known values come from the `WINDOWS` enum on the Rust side
    (`[10, 60, 300, 900, 1800, 3600]`); unknown values fall back to
    `"{N} seconds"` so a future enum extension doesn't crash the
    composer  the substitution still happens, just with a less
    polished string.
    """
    return _LIVE_WINDOW_HUMAN.get(secs, f"{secs} seconds")


def substitute_live_window(text: str, secs: int) -> str:
    """Replace every occurrence of `${LIVE_WINDOW_HUMAN}` in `text`
    with the human-readable string for `secs`.

    Idempotent / no-op when the placeholder isn't present in the
    input (e.g. an older prompt that hasn't been parameterized yet).
    Exposed for `policy/constitution.py` to reuse so both prompt
    files run through one formatter.
    """
    return text.replace(_LIVE_WINDOW_PLACEHOLDER, format_live_window(secs))


# Match `<rule id="..."> ... </rule>` blocks. Anchored to start-of-line
# on the open tag so a literal `<rule id="...">` accidentally appearing
# inside a rule body cannot be mistaken for a real rule boundary.
# `re.DOTALL` lets `.` cross newlines so the body text matches verbatim.
_RULE_RE = re.compile(
    r'^<rule\s+id="(?P<id>[^"]+)">\n(?P<body>.*?)\n</rule>$',
    re.DOTALL | re.MULTILINE,
)


def compose_system_prompt(
    *,
    drop_rule_ids: Iterable[str] = (),
    source_text: str | None = None,
    live_window_secs: int = DEFAULT_LIVE_WINDOW_SECS,
) -> str:
    """Load `system_v4.txt` (or `source_text` if provided for tests),
    parse `<rule id="...">` blocks, drop any whose id is in
    `drop_rule_ids`, substitute the `${LIVE_WINDOW_HUMAN}` placeholder
    with the human string for `live_window_secs`, return the
    assembled prompt.

    With an empty drop set AND the default window, the substitution
    fills the placeholder with "60 seconds"  the same string the
    original prompt file carried before parameterization. So the
    production preset still matches the historical prompt content
    byte-for-byte at run time, even though the source file on disk
    now has a placeholder instead of the literal "60 seconds".

    Args:
        drop_rule_ids: rule ids to remove from the assembled prompt.
            Each id MUST exist in the source file; an unknown id
            raises CompositionError.
        source_text: optional, override the file lookup with this
            string. Used by unit tests so they can pin behavior on
            small fixtures without depending on the live prompt.
        live_window_secs: the live window the snapshot will be
            materialized against; substituted into the
            `${LIVE_WINDOW_HUMAN}` placeholder in the prompt. Default
            60 mirrors the data-plane default and produces "last 60
            seconds" prose. Eval cases that widen the window (e.g.
            900) pass through the value here to keep the agent's
            framing internally consistent with the snapshot it'll
            actually analyze.

    Returns:
        The composed prompt string.

    Raises:
        CompositionError if the source has duplicate rule ids, or if
            `drop_rule_ids` contains an id not present in the source.
    """
    text = source_text if source_text is not None else load_prompt("system_v4")
    drops = frozenset(drop_rule_ids)

    # Parse: collect every rule's id + full match span + body. We
    # walk the matches once so we can detect duplicates (loud failure)
    # AND validate the drop set against known ids (loud failure on
    # typo).
    matches = list(_RULE_RE.finditer(text))
    seen_ids: set[str] = set()
    duplicates: set[str] = set()
    for m in matches:
        rid = m.group("id")
        if rid in seen_ids:
            duplicates.add(rid)
        seen_ids.add(rid)
    if duplicates:
        raise CompositionError(
            f"system_v4.txt has duplicate rule id(s) {sorted(duplicates)!r}; "
            "every <rule id=...> must be unique"
        )

    unknown_drops = drops - seen_ids
    if unknown_drops:
        raise CompositionError(
            f"compose_system_prompt called with unknown drop_rule_ids "
            f"{sorted(unknown_drops)!r}; known ids are {sorted(seen_ids)!r}"
        )

    # Empty drop set: the source goes through verbatim except for
    # the live-window placeholder substitution. With the default
    # window (60s), `${LIVE_WINDOW_HUMAN}` renders as "60 seconds"
    # which IS the literal text the prompt carried before this
    # parameterization, so the production preset stays observably
    # equivalent to the pre-refactor flat file.
    if not drops:
        return substitute_live_window(text, live_window_secs)

    # Drop set non-empty: walk the source and elide each dropped
    # rule's full span, including the single trailing blank line that
    # separates it from the next rule. Falling back on simple slicing
    # keeps the surrounding `<rules>` container, the role/output
    # blocks, and the inter-rule blank lines intact.
    out_parts: list[str] = []
    cursor = 0
    for m in matches:
        if m.group("id") in drops:
            # Eat one trailing blank line (\n\n) if present so we
            # don't leave a double-blank-gap where the rule used to
            # be. Cheap-and-correct because the source uses exactly
            # one blank line between rules.
            tail_end = m.end()
            if text.startswith("\n\n", tail_end):
                tail_end += 1  # skip one of the two newlines
            out_parts.append(text[cursor : m.start()])
            cursor = tail_end
    out_parts.append(text[cursor:])
    # Substitute the live-window placeholder on the assembled output,
    # AFTER rule-drop so a dropped rule that mentioned the placeholder
    # contributes nothing to the substitution scan (and so the
    # substitution operates on whatever text actually ships, not on
    # an intermediate including elided rules).
    return substitute_live_window("".join(out_parts), live_window_secs)


def drops_from_switches(switches: "AgentSwitches") -> frozenset[str]:
    """Compute the set of rule ids to drop from `system_v4.txt` given
    a turn's `AgentSwitches`. Pure helper, no side effects.

    Mapping rules:

    - `defend_chat_template_spoofing` toggles the boundary rail in
      `loop_driver.py` (`reject_if_unsafe_user_question`). When off,
      we ALSO drop the matching prompt rule
      (`defense:chat_template_rejection`) so the model is not told
      about a rail that no longer fires.
    - `defend_persona_swap` and `defend_decode_and_execute` BOTH map
      to dropping `defense:user_question_untrusted`. The rule's
      prose covers persona-swap, fictional-game framings, AND
      decode-and-execute in one tightly coupled paragraph; we
      considered splitting but the framing is shared. Either flag
      off keeps the rule; only when BOTH are off does the rule
      drop. This is the not-quite-1-to-1 mapping documented in the
      #36 plan.
    - `defend_identity_reveal` -> drop `defense:identity`.
    - `defend_off_domain` -> drop `defense:off_domain`.
    - `defend_memo_injection` -> drop `defense:memo_injection`.

    `defend_constitution_judge` does NOT appear here: it gates the
    constitution gate spans, not the prompt content. Its handling
    lives in `loop_driver.py`'s gate-emission sites.
    """
    role = switches.stay_in_role
    drops: set[str] = set()
    if not role.defend_chat_template_spoofing:
        drops.add("defense:chat_template_rejection")
    if not role.defend_persona_swap and not role.defend_decode_and_execute:
        # Both vectors share the same rule; only drop when BOTH are
        # off. If only one is off the prompt still mentions both
        # framings, which is fine: the model just keeps the still-on
        # vector defended at the prompt layer.
        drops.add("defense:user_question_untrusted")
    if not role.defend_identity_reveal:
        drops.add("defense:identity")
    if not role.defend_off_domain:
        drops.add("defense:off_domain")
    if not role.defend_memo_injection:
        drops.add("defense:memo_injection")
    return frozenset(drops)


def known_rule_ids(*, source_text: str | None = None) -> frozenset[str]:
    """Return the set of rule ids declared in the source file.

    Useful for callers that want to validate switch-to-rule mappings
    at startup rather than on the first compose call. Same parser
    contract as `compose_system_prompt`; raises CompositionError on
    duplicate ids.
    """
    text = source_text if source_text is not None else load_prompt("system_v4")
    ids: set[str] = set()
    duplicates: set[str] = set()
    for m in _RULE_RE.finditer(text):
        rid = m.group("id")
        if rid in ids:
            duplicates.add(rid)
        ids.add(rid)
    if duplicates:
        raise CompositionError(
            f"system_v4.txt has duplicate rule id(s) {sorted(duplicates)!r}; "
            "every <rule id=...> must be unique"
        )
    return frozenset(ids)
