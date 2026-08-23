"""Lint: backend NoteTag enum ⊆ frontend system_marker dispatch sets.

The frontend classifies a system_marker's `source` (a NoteTag value) through
the dispatch sets in `ui/web/src/components/timeline/markers.tsx`
(LIFECYCLE_TAGS / MEMORY_SOURCES / NOTE_SOURCES); any source outside them
renders the red UnknownMarkerChip alarm — fail-loud by design, but a NEW
backend tag landing without a frontend branch is exactly the regression class
the user hit as "UNRECOGNIZED SYSTEM_MARKER (FRONTEND NOT ADAPTED)" (#1017).

This test machine-checks the contract in the backend job (milliseconds, no
browser): the backend enum is AST-parsed from `shared/message_kwargs.py` (the
single source of truth), the frontend sets are read from `markers.tsx`, and a
backend member with no frontend branch fails CI immediately.

The frontend side of the same contract — every dispatch-set member renders
its chip and never the alarm — lives in
`ui/web/src/components/timeline.test.tsx` ("Marker contract: every
dispatch-set source renders without the red alarm").

Companion backend-internal contract: `tests/gateway/test_timeline.py`
`TestAvaMsgTypeDispatch` asserts every AvaMsgType member dispatches to its
intended item kind and never hits the HumanMessage catch-all.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MESSAGE_KWARGS = _REPO_ROOT / "shared" / "message_kwargs.py"
_MARKERS_TS = _REPO_ROOT / "ui" / "web" / "src" / "components" / "timeline" / "markers.tsx"


def _note_tag_members() -> set[str]:
    """AST-extract the NoteTag StrEnum member values from shared/message_kwargs.py."""
    tree = ast.parse(_MESSAGE_KWARGS.read_text())
    for node in tree.body:
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "NoteTag"
            and any(isinstance(b, ast.Name) and b.id == "StrEnum" for b in node.bases)
        ):
            return {
                stmt.value.value
                for stmt in node.body
                if isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            }
    raise AssertionError("NoteTag StrEnum not found in shared/message_kwargs.py")


def _frontend_dispatch_members() -> set[str]:
    """Extract the string members of the exported dispatch sets in markers.tsx.

    Read as text (it is TypeScript): each exported const is either an array
    literal (LIFECYCLE_TAGS) or a Set literal (MEMORY_SOURCES / NOTE_SOURCES).
    """
    text = _MARKERS_TS.read_text()
    members: set[str] = set()
    for name in ("LIFECYCLE_TAGS", "MEMORY_SOURCES", "NOTE_SOURCES"):
        # `export const NAME = [ ... ]` or `export const NAME = new Set([ ... ])`
        m = re.search(rf"export const {name} = (?:new Set\()?\[(.*?)\]", text, re.S)
        assert m, f"exported const {name} not found in markers.tsx"
        members.update(re.findall(r'"([^"]+)"', m.group(1)))
    return members


def test_note_tag_and_frontend_dispatch_sets_agree() -> None:
    backend = _note_tag_members()
    frontend = _frontend_dispatch_members()
    assert backend, "NoteTag enum is empty — did the enum move or get renamed?"
    assert frontend, "frontend dispatch sets are empty — did markers.tsx change shape?"
    missing = sorted(backend - frontend)
    assert not missing, (
        "NoteTag member(s) with no frontend branch in markers.tsx — the UI will "
        f"render the red 'unrecognized system_marker' alarm for them (#1017 class): {missing}. "
        "Add a branch in markers.tsx (LIFECYCLE_TAGS / MEMORY_SOURCES / NOTE_SOURCES)."
    )
    stale = sorted(frontend - backend)
    assert not stale, (
        "Stale frontend dispatch member(s) not present in NoteTag — remove or rename them in "
        f"markers.tsx: {stale}."
    )
