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
import inspect
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MESSAGE_KWARGS = _REPO_ROOT / "shared" / "message_kwargs.py"
_MARKERS_TS = _REPO_ROOT / "ui" / "web" / "src" / "components" / "timeline" / "markers.tsx"
_SYSTEM_NOTE_WRITER_DIRS = ("agent", "ava_builtins", "demos")


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


def test_system_note_writers_use_notetag_values() -> None:
    """Framework note writers must use the closed NoteTag vocabulary."""
    for directory in _SYSTEM_NOTE_WRITER_DIRS:
        for path in (_REPO_ROOT / directory).rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=path)
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "system_note_message"
                ):
                    continue
                tag = next((kw.value for kw in node.keywords if kw.arg == "tag"), None)
                assert tag is not None, f"{path.relative_to(_REPO_ROOT)}:{node.lineno} lacks tag="
                is_member = (
                    isinstance(tag, ast.Attribute)
                    and isinstance(tag.value, ast.Name)
                    and tag.value.id == "NoteTag"
                )
                is_validated_inbound_tag = (
                    isinstance(tag, ast.Call)
                    and isinstance(tag.func, ast.Name)
                    and tag.func.id == "_system_note_tag"
                )
                assert is_member or is_validated_inbound_tag, (
                    f"{path.relative_to(_REPO_ROOT)}:{node.lineno} must pass tag=NoteTag.<member> "
                    "or the validated inbound NoteTag helper"
                )


def test_send_system_note_default_tag_is_live_notetag_value() -> None:
    from ava.agents import send_system_note

    default = inspect.signature(send_system_note).parameters["tag"].default
    assert isinstance(default, str)
    assert default in _note_tag_members()


def test_send_system_note_rejects_unknown_notetag_before_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing the SDK guard to forward an unknown tag must fail this test."""
    from ava import agents

    monkeypatch.setattr(agents.ava._boot, "require_actor", lambda: 1)

    def unexpected_gateway_call(*_args: object, **_kwargs: object) -> int:
        pytest.fail("unknown note tag reached the gateway client")

    monkeypatch.setattr(agents._client, "send_system_note", unexpected_gateway_call)
    with pytest.raises(ValueError) as exc_info:
        agents.send_system_note(7, "note", tag="unrecognized")
    assert "task" in str(exc_info.value)
    assert "heartbeat_pause" in str(exc_info.value)
