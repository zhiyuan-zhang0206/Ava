"""Regression guards for the editable-install pointer shared by Ava lifecycle paths."""

from __future__ import annotations

import json
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


def _write_direct_url(source_root: Path, url: str, *, editable: bool = True) -> Path:
    """A direct_url.json matching uv's editable-install record shape."""
    du = (
        source_root
        / ".venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "ava-0.1.5.dist-info"
        / "direct_url.json"
    )
    du.parent.mkdir(parents=True)
    du.write_text(json.dumps({"url": url, "dir_info": {"editable": editable}}))
    return du


def test_poisoned_direct_url_is_repaired_and_emits_warning_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A direct_url naming a worktree is the same poison recorded elsewhere."""
    source_root = tmp_path / "prod" / "source"
    du = _write_direct_url(source_root, (tmp_path / "deleted-worktree").as_uri())
    du.chmod(0o444)
    original = du.read_text()
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def record_emit(*args: object, **kwargs: object) -> None:
        emitted.append((args, kwargs))

    monkeypatch.setattr("shared.telemetry.emit", record_emit)

    editable_install.repair_editable_direct_url(source_root)

    assert json.loads(du.read_text()) == {
        "url": source_root.as_uri(),
        "dir_info": {"editable": True},
    }
    assert stat.S_IMODE(du.stat().st_mode) == 0o444
    assert emitted == [
        (
            ("telemetry", "editable_direct_url_repaired"),
            {
                "level": "warning",
                "source": "converge",
                "attributes": {
                    "direct_url_path": str(du),
                    "poisoned_target": original.strip(),
                    "source_root": str(source_root),
                },
            },
        )
    ]


def test_healthy_direct_url_is_left_unchanged(tmp_path: Path) -> None:
    """A direct_url naming the source root itself must survive a guard pass."""
    source_root = tmp_path / "prod" / "source"
    du = _write_direct_url(source_root, source_root.as_uri())
    original = du.read_text()

    assert editable_install.repair_editable_direct_url(source_root) == ()
    assert du.read_text() == original


def test_exact_allowlisted_dev_clone_direct_url_is_left_unchanged(
    tmp_path: Path,
) -> None:
    """The exact-root allowlist applies to the direct_url record too."""
    source_root = tmp_path / "prod" / "source"
    dev_clone = tmp_path / "Ava"
    dev_clone.mkdir(parents=True)
    du = _write_direct_url(source_root, dev_clone.as_uri())
    original = du.read_text()

    editable_install.repair_editable_direct_url(source_root, allowed_roots=(dev_clone,))

    assert du.read_text() == original


def test_worktree_below_allowlisted_dev_clone_direct_url_is_repaired(
    tmp_path: Path,
) -> None:
    """Allowlisting a clone root must not implicitly allow its disposable worktrees."""
    source_root = tmp_path / "prod" / "source"
    dev_clone = tmp_path / "Ava"
    worktree = dev_clone / ".worktrees" / "feature"
    worktree.mkdir(parents=True)
    du = _write_direct_url(source_root, worktree.as_uri())

    editable_install.repair_editable_direct_url(source_root, allowed_roots=(dev_clone,))

    assert json.loads(du.read_text())["url"] == source_root.as_uri()


def test_unparsable_direct_url_is_repaired(tmp_path: Path) -> None:
    """A truncated or garbage record must not survive without a repair."""
    source_root = tmp_path / "prod" / "source"
    du = (
        source_root
        / ".venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "ava-0.1.5.dist-info"
        / "direct_url.json"
    )
    du.parent.mkdir(parents=True)
    du.write_text("{not json")

    editable_install.repair_editable_direct_url(source_root)

    assert json.loads(du.read_text()) == {
        "url": source_root.as_uri(),
        "dir_info": {"editable": True},
    }


def test_non_editable_direct_url_is_repaired(tmp_path: Path) -> None:
    """A record denying editability contradicts the pointer beside it."""
    source_root = tmp_path / "prod" / "source"
    du = _write_direct_url(source_root, source_root.as_uri(), editable=False)

    editable_install.repair_editable_direct_url(source_root)

    assert json.loads(du.read_text()) == {
        "url": source_root.as_uri(),
        "dir_info": {"editable": True},
    }


def test_repair_editable_install_repairs_pointer_and_direct_url(tmp_path: Path) -> None:
    """The combined entry point leaves neither record pointing at a worktree."""
    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, tmp_path / "deleted-worktree")
    du = _write_direct_url(source_root, (tmp_path / "deleted-worktree").as_uri())

    repairs = editable_install.repair_editable_install(source_root)

    assert pth.read_text() == str(source_root)
    assert json.loads(du.read_text())["url"] == source_root.as_uri()
    assert len(repairs) == 2


def test_repair_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    """The atomic-write repair must not accumulate temp files in site-packages."""
    source_root = tmp_path / "prod" / "source"
    _write_pth(source_root, tmp_path / "deleted-worktree")
    _write_direct_url(source_root, (tmp_path / "deleted-worktree").as_uri())

    editable_install.repair_editable_install(source_root)

    assert list(source_root.rglob("*.tmp")) == []


# ─── read-only violations report (public twin of the repair primitives) ─────


def test_editable_install_violations_healthy_is_empty(tmp_path: Path) -> None:
    """Legal records (prod root + editable URL) report no violations."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)
    _write_pth(source_root, source_root)
    _write_direct_url(source_root, source_root.as_uri())

    assert editable_install.editable_install_violations(source_root) == ()


def test_editable_install_violations_missing_records_are_empty(tmp_path: Path) -> None:
    """A venv with no editable records has nothing to assert (repair: no-op)."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)

    assert editable_install.editable_install_violations(source_root) == ()


def test_editable_install_violations_allowlisted_clone_is_empty(tmp_path: Path) -> None:
    """Exact-root allowlisting applies to the read-only report too."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)
    dev_clone = tmp_path / "Ava"
    _write_pth(source_root, dev_clone)
    _write_direct_url(source_root, dev_clone.as_uri())

    assert (
        editable_install.editable_install_violations(source_root, allowed_roots=(dev_clone,)) == ()
    )


def test_editable_install_violations_reports_poisoned_records(tmp_path: Path) -> None:
    """Both records are reported, each naming the disposable source."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)
    worktree = tmp_path / "deleted-worktree"
    pth = _write_pth(source_root, worktree)
    record = _write_direct_url(source_root, worktree.as_uri())

    violations = editable_install.editable_install_violations(source_root)

    assert len(violations) == 2
    assert str(pth) in violations[0] and str(worktree) in violations[0]
    assert str(record) in violations[1] and str(worktree) in violations[1]


def test_editable_install_violations_unparsable_record_is_reported(
    tmp_path: Path,
) -> None:
    """A record that cannot be parsed cannot be verified — reported."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)
    _write_pth(source_root, source_root)
    record = _write_direct_url(source_root, source_root.as_uri())
    record.write_text("{not json")

    violations = editable_install.editable_install_violations(source_root)

    assert len(violations) == 1 and str(record) in violations[0]
