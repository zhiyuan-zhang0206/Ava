"""The converge preserve signal — a preserved destination reports telemetry."""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.commands._rendered_file import write_rendered_guarded


def test_preserve_hit_reports_converge_file_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-edited destination is kept AND reported as a converge_file_preserved event."""
    emitted: list[tuple[object, ...]] = []

    def record_emit(*args: object, **kwargs: object) -> None:
        emitted.append((*args, kwargs))

    monkeypatch.setattr("shared.telemetry.emit", record_emit)

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
