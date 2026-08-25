"""Regression guards for the editable-install pointer shared by Ava lifecycle paths."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from shared import editable_install


def _write_pth(source_root: Path, target: Path) -> Path:
    pth = source_root / ".venv" / "lib" / "python3.12" / "site-packages" / "_editable_impl_ava.pth"
    pth.parent.mkdir(parents=True)
    pth.write_text(str(target))
    return pth


def test_poisoned_missing_target_is_repaired_and_emits_warning_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dangling target must not survive a guard pass without an audit event."""
    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, tmp_path / "deleted-worktree")
    pth.chmod(0o444)
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def record_emit(*args: object, **kwargs: object) -> None:
        emitted.append((args, kwargs))

    monkeypatch.setattr("shared.telemetry.emit", record_emit)

    editable_install.repair_editable_ava_pth(source_root)

    assert pth.read_text() == str(source_root)
    assert stat.S_IMODE(pth.stat().st_mode) == 0o444
    assert emitted == [
        (
            ("telemetry", "editable_pth_repaired"),
            {
                "level": "warning",
                "source": "converge",
                "attributes": {
                    "pth_path": str(pth),
                    "poisoned_target": str(tmp_path / "deleted-worktree"),
                    "source_root": str(source_root),
                },
            },
        )
    ]


def test_worktree_below_allowlisted_dev_clone_is_still_repaired(tmp_path: Path) -> None:
    """Allowlisting a clone root must not implicitly allow its disposable worktrees."""
    source_root = tmp_path / "prod" / "source"
    dev_clone = tmp_path / "Ava"
    worktree = dev_clone / ".worktrees" / "feature"
    worktree.mkdir(parents=True)
    pth = _write_pth(source_root, worktree)

    editable_install.repair_editable_ava_pth(source_root, allowed_roots=(dev_clone,))

    assert pth.read_text() == str(source_root)


def test_exact_allowlisted_dev_clone_target_is_left_unchanged(tmp_path: Path) -> None:
    """The allowlist is exact-root based, so an approved stable clone remains legal."""
    source_root = tmp_path / "prod" / "source"
    dev_clone = tmp_path / "Ava"
    dev_clone.mkdir(parents=True)
    pth = _write_pth(source_root, dev_clone)

    editable_install.repair_editable_ava_pth(source_root, allowed_roots=(dev_clone,))

    assert pth.read_text() == str(dev_clone)
