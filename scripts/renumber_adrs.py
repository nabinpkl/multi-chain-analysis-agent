#!/usr/bin/env python3
"""Renumber ADRs in architecture-decisions/ to be contiguous from 01.

Closes any gaps in the sequence (e.g. missing 03) by renaming files
and updating every reference across the repo in one atomic pass.

Safe because:
- Renames go in ascending order of OLD num, and every rename moves
  a file DOWN to a slot that was just vacated (or was empty).
- Reference updates use a callback for "ADR N" patterns so all
  substitutions happen simultaneously, no collisions.
- Filename references are exact-match per known basename.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent  # /tmp -> repo? No, fix below
REPO = Path("/Users/nabin/projects/multi-chain-analysis-engine")
ADR_DIR = REPO / "architecture-decisions"

# Where to scan for references that may need updating.
SCAN_DIRS = [REPO / "architecture-decisions", REPO / "docs"]


def list_adrs() -> list[Path]:
    """All NN-*.md files in ADR_DIR sorted by leading number."""
    return sorted(
        [p for p in ADR_DIR.iterdir() if re.match(r"\d{2}-.*\.md$", p.name)],
        key=lambda p: int(p.name[:2]),
    )


def build_maps(files: list[Path]) -> tuple[dict[str, str], dict[int, int]]:
    """Return (basename_map, num_map) for the files that need renaming.

    basename_map: 'OLD-name.md' -> 'NEW-name.md'  (only files whose num changes)
    num_map:      OLD_int       -> NEW_int       (same set, by number)
    """
    basename_map: dict[str, str] = {}
    num_map: dict[int, int] = {}
    for new_idx, f in enumerate(files, start=1):
        old_num = int(f.name[:2])
        if old_num != new_idx:
            new_basename = f"{new_idx:02d}-{f.name[3:]}"
            basename_map[f.name] = new_basename
            num_map[old_num] = new_idx
    return basename_map, num_map


def rename_files(basename_map: dict[str, str]) -> None:
    """git mv each renamed file. Order: ascending old num so destinations are
    always free (we move N -> N-1, then N+1 -> N, etc.)."""
    for old, new in sorted(basename_map.items(), key=lambda kv: int(kv[0][:2])):
        old_path = ADR_DIR / old
        new_path = ADR_DIR / new
        subprocess.run(
            ["git", "mv", str(old_path), str(new_path)],
            cwd=REPO, check=True,
        )
        print(f"  git mv {old} -> {new}")


def update_references(basename_map: dict[str, str], num_map: dict[int, int]) -> None:
    """Walk every .md file under SCAN_DIRS and update references."""

    def replace_adr_num(m: re.Match) -> str:
        old = int(m.group(1))
        new = num_map.get(old, old)
        return f"ADR {new}"

    files_touched = 0
    for scan_dir in SCAN_DIRS:
        for md in scan_dir.rglob("*.md"):
            text = md.read_text()
            original = text

            # 1) "ADR N" references (handle as a callback so all
            #    substitutions are simultaneous; no double-rewrite risk).
            text = re.sub(r"\bADR (\d+)\b", replace_adr_num, text)

            # 2) Filename references. Replace exact known basenames only.
            for old_name, new_name in basename_map.items():
                text = text.replace(old_name, new_name)

            if text != original:
                md.write_text(text)
                rel = md.relative_to(REPO)
                print(f"  refs updated in {rel}")
                files_touched += 1

    print(f"\n{files_touched} file(s) had references updated.")


def main() -> None:
    files = list_adrs()
    if not files:
        print("No ADR files found.")
        sys.exit(1)

    print(f"Found {len(files)} ADR files:")
    for f in files:
        print(f"  {f.name}")

    basename_map, num_map = build_maps(files)
    if not basename_map:
        print("\nAll ADRs already contiguous. Nothing to do.")
        return

    print(f"\nWill rename {len(basename_map)} files to close gaps:")
    for old, new in sorted(basename_map.items(), key=lambda kv: int(kv[0][:2])):
        print(f"  {old}  ->  {new}")

    print(f"\nADR number remap (for 'ADR N' text references):")
    for old, new in sorted(num_map.items()):
        print(f"  ADR {old}  ->  ADR {new}")

    if "--apply" not in sys.argv:
        print("\n(dry run; pass --apply to execute)")
        return

    print("\n--- renaming files ---")
    rename_files(basename_map)

    print("\n--- updating references ---")
    update_references(basename_map, num_map)

    print("\nDone. Run `git status` to inspect, `git diff` to review.")


if __name__ == "__main__":
    main()
