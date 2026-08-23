#!/usr/bin/env python3
"""Lint the bidirectional ``NoteTag`` / frontend system_marker contract.

Every NoteTag enum value must have a branch in the frontend dispatch sets, or
the tag falls through to ``UnknownMarkerChip``. Conversely, every string in
``LIFECYCLE_TAGS``, ``MEMORY_SOURCES``, or ``NOTE_SOURCES`` must be a live
NoteTag value; otherwise a removed or renamed backend tag leaves stale frontend
behaviour. Both directions fail the hook with a clear message.

Trigger: runs on either ``shared/message_kwargs.py`` or ``markers.tsx``
changes (see ``.pre-commit-config.yaml``). No filenames passed; the script
reads both files at their canonical paths from the repo root.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PYTHON_SRC = REPO_ROOT / "shared" / "message_kwargs.py"
TS_SRC = REPO_ROOT / "ui" / "web" / "src" / "components" / "timeline" / "markers.tsx"


def extract_note_tags(path: Path) -> list[str]:
    """Extract every member value from the ``NoteTag(StrEnum)`` class body.

    Each member is a line like ``    NAME = "value"``. We capture the
    double-quoted string on the right side.
    """
    text = path.read_text()
    # Find the class NoteTag block — from class line to next blank line +
    # non-indented line (or end of file)
    m = re.search(
        r"^class NoteTag\(StrEnum\):\s*\n(.*?)(?=^\S|\Z)",
        text,
        re.DOTALL | re.MULTILINE,
    )
    if not m:
        print("ERROR: could not find `class NoteTag(StrEnum)` in", PYTHON_SRC, file=sys.stderr)
        sys.exit(1)

    body = m.group(1)
    # Each member: optional docstring, then NAME = "value"
    values: list[str] = []
    for line in body.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith(('"""', "#")):
            continue
        # Match NAME = "value"
        m2 = re.match(r'^\w+\s*=\s*"([^"]+)"', stripped)
        if m2:
            values.append(m2.group(1))
    return values


def check_coverage(note_tags: list[str], ts_path: Path) -> list[str]:
    """Return the subset of *note_tags* not found in the TypeScript source."""
    ts_text = ts_path.read_text()
    missing: list[str] = []
    for tag in note_tags:
        if f'"{tag}"' not in ts_text and f"'{tag}'" not in ts_text:
            missing.append(tag)
    return missing


def extract_dispatch_tags(path: Path) -> set[str]:
    """Extract string members from the frontend's three NoteTag dispatch sets."""
    text = path.read_text()
    values: set[str] = set()
    for name in ("LIFECYCLE_TAGS", "MEMORY_SOURCES", "NOTE_SOURCES"):
        match = re.search(
            rf"export const {name} = (?:new Set\()?\[(.*?)\]",
            text,
            re.DOTALL,
        )
        if match is None:
            print(f"ERROR: could not find {name} in {TS_SRC}", file=sys.stderr)
            sys.exit(1)
        values.update(re.findall(r"[\"']([^\"']+)[\"']", match.group(1)))
    return values


def check_stale_dispatch_tags(note_tags: list[str], ts_path: Path) -> list[str]:
    """Return dispatch-set values that have no current NoteTag member."""
    return sorted(extract_dispatch_tags(ts_path) - set(note_tags))


def main() -> None:
    if not PYTHON_SRC.exists():
        print(f"ERROR: {PYTHON_SRC} not found", file=sys.stderr)
        sys.exit(1)
    if not TS_SRC.exists():
        print(f"ERROR: {TS_SRC} not found", file=sys.stderr)
        sys.exit(1)

    note_tags = extract_note_tags(PYTHON_SRC)
    if not note_tags:
        print("ERROR: no NoteTag values extracted", file=sys.stderr)
        sys.exit(1)

    missing = check_coverage(note_tags, TS_SRC)
    if missing:
        print(
            f"ERROR: {len(missing)} NoteTag value(s) not found in markers.tsx dispatch:",
            file=sys.stderr,
        )
        for tag in missing:
            print(f"  - {tag}", file=sys.stderr)
        print(
            "\nEach NoteTag added to shared/message_kwargs.py must have a matching branch in",
            file=sys.stderr,
        )
        print(
            f"  {TS_SRC.relative_to(REPO_ROOT)}",
            file=sys.stderr,
        )
        print(
            "  otherwise it renders as the red 'Unrecognized system_marker' alarm.",
            file=sys.stderr,
        )
        sys.exit(1)

    stale = check_stale_dispatch_tags(note_tags, TS_SRC)
    if stale:
        print(
            f"ERROR: {len(stale)} stale system_marker dispatch value(s) not in NoteTag:",
            file=sys.stderr,
        )
        for tag in stale:
            print(f"  - {tag}", file=sys.stderr)
        print(
            "\nRemove or rename the stale member in LIFECYCLE_TAGS, MEMORY_SOURCES, or NOTE_SOURCES.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"OK: NoteTag and markers.tsx dispatch sets agree on {len(note_tags)} values")


if __name__ == "__main__":
    main()
