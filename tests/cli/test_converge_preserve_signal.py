"""The converge preserve signal — every preserved destination reports telemetry.

Locks the emission faces added for the #3689 failure mode (a hand-edited LGTM
dashboard sat frozen for three rollouts because the preserve warning lived only
in converge output): the rendered-file guard, and the cluster-extension
materializer's kept-local-edits loop (task #3871).
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.commands._converge_extensions import materialize_cluster_extensions
from cli.commands._rendered_file import write_rendered_guarded
from shared import db, extension_materialize, paths


def _capture_emit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    emitted: list[tuple[object, ...]] = []

    def record_emit(*args: object, **kwargs: object) -> None:
        emitted.append((*args, kwargs))

    monkeypatch.setattr("shared.telemetry.emit", record_emit)
    return emitted


def test_preserve_hit_reports_converge_file_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-edited rendered destination is kept AND reported as an event."""
    emitted = _capture_emit(monkeypatch)

    target = tmp_path / "rendered.yaml"
    hashes = tmp_path / "rendered-hashes.json"
    assert write_rendered_guarded(target, "v1", hashes, "k", surface="test-surface") is None

    target.write_text("hand-edited", encoding="utf-8")
    warning = write_rendered_guarded(target, "v2", hashes, "k", surface="test-surface")

    assert warning is not None and "modified locally" in warning
    assert target.read_text(encoding="utf-8") == "hand-edited"
    assert emitted == [
        (
            "telemetry",
            "converge_file_preserved",
            {
                "level": "warning",
                "source": "converge",
                "attributes": {"path": str(target), "key": "k", "surface": "test-surface"},
            },
        )
    ]


class _PoolStub:
    """Minimal pool shape for the CLI reader: pool context + connection context."""

    def __enter__(self) -> _PoolStub:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def connection(self) -> contextlib.AbstractContextManager[object]:
        return contextlib.nullcontext(object())


def test_extensions_kept_local_edits_report_converge_file_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kept local extension edit is reported next to its stderr warning."""
    emitted = _capture_emit(monkeypatch)
    monkeypatch.setattr(db, "pool", _PoolStub)
    monkeypatch.setattr(paths, "skills_dir", lambda: tmp_path / "skills")

    def kept_edit(_conn: object, *, dest_root: Path) -> SimpleNamespace:
        assert dest_root == tmp_path / "skills"
        return SimpleNamespace(
            landed=[], updated=[], kept_local_edits=["edit-demo"], missing_blob=[]
        )

    monkeypatch.setattr(extension_materialize, "materialize_skills", kept_edit)

    materialize_cluster_extensions()

    assert emitted == [
        (
            "telemetry",
            "converge_file_preserved",
            {
                "level": "warning",
                "source": "converge",
                "attributes": {
                    "path": str(tmp_path / "skills" / "edit-demo"),
                    "key": "edit-demo",
                    "surface": "extensions",
                },
            },
        )
    ]
