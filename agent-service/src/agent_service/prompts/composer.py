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
) -> str:
    """Load `system_v4.txt` (or `source_text` if provided for tests),
    parse `<rule id="...">` blocks, drop any whose id is in
    `drop_rule_ids`, return the assembled prompt.

    With an empty drop set the output is the source file verbatim
    (no transformation; we return the original string). This is the
    regression guard: production preset stays byte-identical to the
    file on disk.

    Args:
        drop_rule_ids: rule ids to remove from the assembled prompt.
            Each id MUST exist in the source file; an unknown id
            raises CompositionError.
        source_text: optional, override the file lookup with this
            string. Used by unit tests so they can pin behavior on
            small fixtures without depending on the live prompt.

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

    # Empty drop set: return the source verbatim. No assembly, no
    # whitespace normalization. This is the byte-identity guarantee
    # for the production preset.
    if not drops:
        return text

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
    return "".join(out_parts)


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
