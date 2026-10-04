"""Private local-storage permissions and atomic write guarantees."""

from __future__ import annotations

import os
import re
import shutil
import socket
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from base.host import private_storage


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


_SUMMARY = (
    "private storage convergence skipped {n} node(s) under {path} "
    "(symlink={symlinks}, foreign_owned={foreign_owned}, "
    "non_regular={non_regular}; node_modules dirs not descended={node_modules}) "
    "— first: {examples}"
)


class _Captured:
    """`private_storage.logger` replacement recording debug and warning calls."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.debugs: list[tuple[str, dict[str, object]]] = []
        self.warnings: list[tuple[str, dict[str, object]]] = []
        monkeypatch.setattr(
            private_storage,
            "logger",
            SimpleNamespace(debug=self._record_debug, warning=self._record_warning),
            raising=False,
        )

    def _record_debug(self, message: str, **kwargs: object) -> None:
        self.debugs.append((message, kwargs))

    def _record_warning(self, message: str, **kwargs: object) -> None:
        self.warnings.append((message, kwargs))


def test_private_dir_rejects_symlink(tmp_path: Path) -> None:
    """A pre-placed symlink must never become a private storage directory."""
    target = tmp_path / "private"
    destination = tmp_path / "elsewhere"
    target.symlink_to(destination, target_is_directory=True)

    with pytest.raises(RuntimeError, match=rf"{re.escape(str(target))}.*symlink"):
        private_storage.ensure_private_dir(target)
    assert not destination.exists()


def test_private_dir_rejects_foreign_owner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A directory owned by another account cannot hold this process's secrets."""
    target = tmp_path / "private"
    target.mkdir()
    owner = os.geteuid()
    monkeypatch.setattr(private_storage.os, "geteuid", lambda: owner + 1)

    with pytest.raises(
        RuntimeError, match=rf"{re.escape(str(target))}.*not owned by the current user"
    ):
        private_storage.ensure_private_dir(target)


def test_converge_skips_foreign_nodes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Foreign-owned nodes are debug-logged, summarized, and left unchanged."""
    target = tmp_path / "tree"
    ours_dir = target / "ours"
    ours_dir.mkdir(parents=True)
    foreign_file = ours_dir / "secret"
    foreign_file.write_bytes(b"secret")
    foreign_file.chmod(0o400)  # marker: reads as foreign
    foreign_dir = target / "foreign"
    foreign_dir.mkdir()
    foreign_dir.chmod(0o500)  # marker: reads as foreign
    cap = _Captured(monkeypatch)

    def _foreign(stat_result: os.stat_result) -> bool:
        return stat_result.st_mode & 0o777 in (0o400, 0o500)

    with patch.object(private_storage, "_is_foreign_owned", side_effect=_foreign):
        private_storage.converge_private_tree(target)

    # foreign nodes left untouched, ours dir converged to owner-only mode
    assert _mode(foreign_file) == 0o400
    assert _mode(foreign_dir) == 0o500
    assert _mode(ours_dir) == 0o700
    # iterdir order is filesystem-dependent: assert the set of skipped nodes.
    assert {(message, str(kwargs["path"])) for message, kwargs in cap.debugs} == {
        ("private storage convergence skipped foreign-owned path {path}", str(foreign_file)),
        ("private storage convergence skipped foreign-owned path {path}", str(foreign_dir)),
    }
    assert len(cap.warnings) == 1
    message, kwargs = cap.warnings[0]
    assert message == _SUMMARY
    assert kwargs["n"] == 2
    assert kwargs["foreign_owned"] == 2
    assert kwargs["symlinks"] == 0
    assert kwargs["non_regular"] == 0
    assert kwargs["node_modules"] == 0
    examples = str(kwargs["examples"])
    assert str(foreign_file) in examples and str(foreign_dir) in examples


def test_is_foreign_owned_is_uid_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ownership follows the uid alone; a foreign gid does not make a path foreign."""

    def _stat(uid: int, gid: int) -> os.stat_result:
        return os.stat_result((0o100644, 0, 0, 1, uid, gid, 0, 0, 0, 0))

    ours = _stat(os.geteuid(), os.geteuid() + 1)
    assert private_storage._is_foreign_owned(ours) is False

    theirs = _stat(os.geteuid() + 1, os.geteuid())
    assert private_storage._is_foreign_owned(theirs) is True

    monkeypatch.setattr(private_storage.os, "name", "nt")
    assert private_storage._is_foreign_owned(theirs) is False


def test_private_dir_repairs_mode_drift(tmp_path: Path) -> None:
    """A lax directory from an older umask is tightened before reuse."""
    target = tmp_path / "private"
    target.mkdir()
    target.chmod(0o755)

    assert private_storage.ensure_private_dir(target) == target
    assert _mode(target) == 0o700


def test_private_file_repairs_mode_drift(tmp_path: Path) -> None:
    """An existing secret file is tightened without changing its content."""
    target = tmp_path / "secret"
    target.write_bytes(b"secret")
    target.chmod(0o644)

    private_storage.ensure_private_file(target)

    assert target.read_bytes() == b"secret"
    assert _mode(target) == 0o600


def test_private_file_preserves_owner_execute_permission(tmp_path: Path) -> None:
    """An executable file remains executable while its mode is tightened."""
    target = tmp_path / "hook"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o744)

    private_storage.ensure_private_file(target)

    assert _mode(target) == 0o700


def test_private_tree_recursively_repairs_existing_mode_drift(tmp_path: Path) -> None:
    """Converge makes every existing private-tree node owner-only."""
    root = tmp_path / "private"
    nested = root / "agent" / "artifacts"
    nested.mkdir(parents=True)
    payload = nested / "result.txt"
    payload.write_text("secret")
    for directory in (root, root / "agent", nested):
        directory.chmod(0o755)
    payload.chmod(0o644)

    assert private_storage.converge_private_tree(root) == root

    assert _mode(root) == 0o700
    assert _mode(root / "agent") == 0o700
    assert _mode(nested) == 0o700
    assert _mode(payload) == 0o600


def test_private_tree_skips_nested_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Converge leaves a link and its target untouched while repairing its tree."""
    root = tmp_path / "private"
    root.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    target.chmod(0o755)
    link = root / "link"
    link.symlink_to(target, target_is_directory=True)
    cap = _Captured(monkeypatch)

    assert private_storage.converge_private_tree(root) == root

    assert link.is_symlink()
    assert _mode(target) == 0o755
    assert cap.debugs == [
        ("private storage convergence skipped symlink {path}", {"path": link}),
    ]
    assert cap.warnings == [
        (
            _SUMMARY,
            {
                "n": 1,
                "path": root,
                "symlinks": 1,
                "foreign_owned": 0,
                "non_regular": 0,
                "node_modules": 0,
                "examples": f"{link} (symlink)",
            },
        )
    ]


def test_private_write_replaces_existing_content_without_permissive_intermediate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A replacement keeps the prior file intact until a private temp file replaces it."""
    target = tmp_path / "secret"
    target.write_bytes(b"old")
    target.chmod(0o644)
    real_replace = private_storage.os.replace
    seen: dict[str, Path] = {}

    def _replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        seen["temporary"] = source_path
        assert source_path.parent == target.parent
        assert _mode(source_path) == 0o600
        assert destination_path.read_bytes() == b"old"
        real_replace(source, destination)

    monkeypatch.setattr(private_storage.os, "replace", _replace)
    private_storage.write_private_bytes(target, b"new")

    assert seen["temporary"] != target
    assert target.read_bytes() == b"new"
    assert _mode(target) == 0o600


def test_private_write_keeps_previous_complete_value_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "secret"
    target.write_bytes(b"old-complete")

    def _fail_replace(_source: object, _destination: object) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(private_storage.os, "replace", _fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        private_storage.write_private_bytes(target, b"new-complete")

    assert target.read_bytes() == b"old-complete"
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is POSIX-only")
def test_private_write_fsyncs_payload_and_parent_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[int] = []
    real_fsync = private_storage.os.fsync

    def _fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(private_storage.os, "fsync", _fsync)
    private_storage.write_private_bytes(tmp_path / "secret", b"durable")

    assert len(calls) == 2


def test_converge_skips_foreign_owned_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A foreign-owned root is warned about and left untouched with its subtree."""
    root = tmp_path / "private"
    root.mkdir()
    root.chmod(0o755)
    child = root / "secret"
    child.write_bytes(b"secret")
    child.chmod(0o644)
    cap = _Captured(monkeypatch)

    def _foreign(_stat_result: os.stat_result) -> bool:
        return True

    monkeypatch.setattr(private_storage, "_is_foreign_owned", _foreign)

    assert private_storage.converge_private_tree(root) == root

    assert _mode(root) == 0o755  # not converged: the owner alone can chmod
    assert _mode(child) == 0o644  # subtree not visited either
    # The root itself is one node, not a flood: the single warning is its own,
    # never a summary.
    assert cap.warnings == [
        ("private storage convergence skipped foreign-owned path {path}", {"path": root})
    ]
    assert cap.debugs == []


@pytest.mark.skipif(os.name == "nt", reason="unix sockets are POSIX-only")
def test_private_tree_skips_live_unix_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """A service socket is warned about and left in place; the run completes.

    The 2026-09-12 host outage: a dead workspace socket aborted the whole
    converge, failing the updater. The tree root sits under /tmp because
    pytest's tmp_path on macOS breaks the 104-byte AF_UNIX path limit.
    """
    root = Path(tempfile.mkdtemp(prefix="ava-converge-", dir="/tmp"))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        root.chmod(0o755)
        payload = root / "result.txt"
        payload.write_text("secret")
        payload.chmod(0o644)
        socket_path = root / "app.sock"
        server.bind(str(socket_path))
        cap = _Captured(monkeypatch)

        assert private_storage.converge_private_tree(root) == root

        assert stat.S_ISSOCK(socket_path.lstat().st_mode)  # still bound, not unlinked
        assert _mode(root) == 0o700
        assert _mode(payload) == 0o600  # the run continued past the socket
        assert cap.debugs == [
            ("private storage convergence skipped non-regular file {path}", {"path": socket_path}),
        ]
        assert cap.warnings == [
            (
                _SUMMARY,
                {
                    "n": 1,
                    "path": root,
                    "symlinks": 0,
                    "foreign_owned": 0,
                    "non_regular": 1,
                    "node_modules": 0,
                    "examples": f"{socket_path} (non-regular)",
                },
            )
        ]
    finally:
        server.close()
        shutil.rmtree(root)


@pytest.mark.skipif(os.name == "nt", reason="fifos are POSIX-only")
def test_private_tree_skips_fifo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A named pipe is warned about and left in place; the run completes."""
    root = tmp_path / "private"
    root.mkdir()
    fifo = root / "queue"
    os.mkfifo(fifo)
    cap = _Captured(monkeypatch)

    assert private_storage.converge_private_tree(root) == root

    assert stat.S_ISFIFO(fifo.lstat().st_mode)
    assert _mode(root) == 0o700
    assert cap.debugs == [
        ("private storage convergence skipped non-regular file {path}", {"path": fifo}),
    ]
    assert cap.warnings == [
        (
            _SUMMARY,
            {
                "n": 1,
                "path": root,
                "symlinks": 0,
                "foreign_owned": 0,
                "non_regular": 1,
                "node_modules": 0,
                "examples": f"{fifo} (non-regular)",
            },
        )
    ]


def test_converge_does_not_descend_node_modules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A node_modules subtree is left alone: the directory itself is converged,
    its contents are neither repaired nor reported, and the pre-stop scan
    agrees (it does not descend there either)."""
    root = tmp_path / "private"
    modules = root / "pkg" / "node_modules"
    inner = modules / "dep"
    inner.mkdir(parents=True)
    payload = inner / "index.js"
    payload.write_bytes(b"module.exports = {};\n")
    payload.chmod(0o644)  # stays: nothing under node_modules is walked
    (inner / "linked").symlink_to(inner / "index.js")
    fifo = inner / "queue"
    if os.name != "nt":
        os.mkfifo(fifo)
    top = root / "keep.txt"
    top.write_bytes(b"secret")
    top.chmod(0o644)
    cap = _Captured(monkeypatch)

    assert private_storage.converge_private_tree(root) == root

    assert _mode(modules) == 0o700  # the directory itself is converged
    assert _mode(payload) == 0o644  # contents are not walked or repaired
    assert _mode(top) == 0o600
    assert cap.debugs == []  # nothing was skipped in the walked tree
    assert cap.warnings == []  # and a pruned node_modules alone is not noise
    assert private_storage.scan_non_regular_nodes(root) == []


def test_summary_caps_examples_and_counts_every_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one summary line carries the full count and only the first few
    example paths."""
    root = tmp_path / "private"
    root.mkdir()
    links: list[Path] = []
    for index in range(5):
        link = root / f"link{index}"
        link.symlink_to(tmp_path / "missing-target")
        links.append(link)
    cap = _Captured(monkeypatch)

    assert private_storage.converge_private_tree(root) == root

    assert len(cap.warnings) == 1
    message, kwargs = cap.warnings[0]
    assert message == _SUMMARY
    assert kwargs["n"] == 5
    assert kwargs["symlinks"] == 5
    examples = str(kwargs["examples"])
    assert examples.endswith("…")
    assert sum(str(link) in examples for link in links) == 3


def test_scan_non_regular_nodes_matches_what_converge_skips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pre-stop scan reports exactly what converge would warn about: a nested
    FIFO is found; a symlink and its target are not (converge does not follow them)."""
    root = tmp_path / "private"
    nested = root / "agent"
    nested.mkdir(parents=True)
    (nested / "result.txt").write_text("secret")
    fifo = nested / "queue"
    os.mkfifo(fifo)
    target = tmp_path / "outside"
    target.mkdir()
    (root / "link").symlink_to(target, target_is_directory=True)

    assert private_storage.scan_non_regular_nodes(root) == [fifo]

    cap = _Captured(monkeypatch)
    assert private_storage.converge_private_tree(root) == root
    assert (
        "private storage convergence skipped non-regular file {path}",
        {"path": fifo},
    ) in cap.debugs
    assert len(cap.warnings) == 1
    assert cap.warnings[0][1]["symlinks"] == 1  # the symlink is skipped, the FIFO reported
    assert cap.warnings[0][1]["non_regular"] == 1
    assert cap.warnings[0][1]["n"] == 2


def test_scan_non_regular_nodes_missing_root_is_empty(tmp_path: Path) -> None:
    assert private_storage.scan_non_regular_nodes(tmp_path / "absent") == []


@pytest.mark.skipif(os.name == "nt", reason="fifos are POSIX-only")
def test_scan_non_regular_nodes_does_not_follow_a_symlinked_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    os.mkfifo(target / "queue")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    assert private_storage.scan_non_regular_nodes(link) == []


def test_private_tree_root_problem_matches_converges_refusals(tmp_path: Path) -> None:
    """The read-only predicate names converge's own refusal (symlink / not a
    directory) and, like converge, does not refuse a missing root."""
    root = tmp_path / "root"
    assert private_storage.private_tree_root_problem(root) is None

    root.write_text("file")
    assert private_storage.private_tree_root_problem(root) == "is not a directory"

    root.unlink()
    target = tmp_path / "target"
    target.mkdir()
    root.symlink_to(target, target_is_directory=True)
    assert private_storage.private_tree_root_problem(root) == "is a symlink"


def test_private_file_problem_matches_the_writer_refusal(tmp_path: Path) -> None:
    path = tmp_path / "marker"
    assert private_storage.private_file_problem(path) is None

    path.symlink_to(tmp_path / "elsewhere")
    assert private_storage.private_file_problem(path) == "is a symlink"


@pytest.mark.skipif(os.name == "nt", reason="fifos are POSIX-only")
def test_private_file_problem_sees_a_fifo(tmp_path: Path) -> None:
    path = tmp_path / "marker"
    os.mkfifo(path)
    assert private_storage.private_file_problem(path) == "is not a regular file"
