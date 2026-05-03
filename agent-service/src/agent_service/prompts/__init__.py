"""Prompt loader. Prompt files in this directory are byte-copies of
the corresponding Rust files under `backend/src/agent/`. Do not edit
the copies; they're checked under `tests/unit/test_prompts_loaded.py`
to be byte-equal to the Rust sources, and a divergence fails CI.

When iterating on prompts:
1. Edit the Rust source (`backend/src/agent/prompt_v4.txt` or
   `policy_prompt_v4.txt`)
2. Run `just sync-prompts` (or copy by hand) to refresh the Python
   copies
3. Re-run tests

Phase II will load these via `load_prompt('system_v4')` to feed the
Pydantic AI agent's `system_prompt`. Phase IV uses `load_prompt
('policy_v4')` for the constitution gate's prompt.
"""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR: Path = Path(__file__).parent


def load_prompt(name: str) -> str:
    """Read a prompt file by name (without `.txt`). Raises
    `FileNotFoundError` if missing so a typo surfaces fast.

    Returns the file contents verbatim, preserving newlines and
    trailing whitespace. Pydantic AI's `system_prompt` accepts the
    raw string."""
    path = PROMPTS_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8")
