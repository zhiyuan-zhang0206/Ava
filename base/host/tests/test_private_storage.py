from __future__ import annotations

import os
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


def test_converge_skips_foreign_nodes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "tree"
    ours_dir = target / "ours"
    ours_dir.mkdir(parents=True)
    foreign_file = ours_dir / "secret"
    foreign_file.write_bytes(b"secret")
    foreign_file.chmod(0o400)
    foreign_dir = target / "foreign"
    foreign_dir.mkdir()
    foreign_dir.chmod(0o500)
    cap = _Captured(monkeypatch)

    def _foreign(stat_result: os.stat_result) -> bool:
        return stat_result.st_mode & 0o777 in (0o400, 0o500)

    with patch.object(private_storage, "is_foreign_owned", side_effect=_foreign):
        private_storage.converge_private_tree(target)

    assert _mode(foreign_file) == 0o400
    assert _mode(foreign_dir) == 0o500
    assert _mode(ours_dir) == 0o700
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


def test_private_file_repairs_mode_drift(tmp_path: Path) -> None:
    target = tmp_path / "secret"
    target.write_bytes(b"secret")
    target.chmod(0o644)

    private_storage.ensure_private_file(target)

    assert target.read_bytes() == b"secret"
    assert _mode(target) == 0o600


def test_private_file_preserves_owner_execute_permission(tmp_path: Path) -> None:
    target = tmp_path / "hook"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o744)

    private_storage.ensure_private_file(target)

    assert _mode(target) == 0o700


def test_private_tree_recursively_repairs_existing_mode_drift(tmp_path: Path) -> None:
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
    root = tmp_path / "private"
    root.mkdir()
    root.chmod(0o755)
    child = root / "secret"
    child.write_bytes(b"secret")
    child.chmod(0o644)
    cap = _Captured(monkeypatch)

    def _foreign(_stat_result: os.stat_result) -> bool:
        return True

    monkeypatch.setattr(private_storage, "is_foreign_owned", _foreign)

    assert private_storage.converge_private_tree(root) == root

    assert _mode(root) == 0o755
    assert _mode(child) == 0o644
    assert cap.warnings == [
        ("private storage convergence skipped foreign-owned path {path}", {"path": root})
    ]
    assert cap.debugs == []


def test_private_tree_skips_live_unix_socket(monkeypatch: pytest.MonkeyPatch) -> None:
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

        assert stat.S_ISSOCK(socket_path.lstat().st_mode)
        assert _mode(root) == 0o700
        assert _mode(payload) == 0o600
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


def test_private_tree_skips_fifo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    root = tmp_path / "private"
    modules = root / "pkg" / "node_modules"
    inner = modules / "dep"
    inner.mkdir(parents=True)
    payload = inner / "index.js"
    payload.write_bytes(b"module.exports = {};\n")
    payload.chmod(0o644)
    (inner / "linked").symlink_to(inner / "index.js")
    fifo = inner / "queue"
    os.mkfifo(fifo)
    top = root / "keep.txt"
    top.write_bytes(b"secret")
    top.chmod(0o644)
    cap = _Captured(monkeypatch)

    assert private_storage.converge_private_tree(root) == root

    assert _mode(modules) == 0o700
    assert _mode(payload) == 0o644
    assert _mode(top) == 0o600
    assert cap.debugs == []
    assert cap.warnings == []
    assert private_storage.scan_non_regular_nodes(root) == []


def test_summary_caps_examples_and_counts_every_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
    assert cap.warnings[0][1]["symlinks"] == 1
    assert cap.warnings[0][1]["non_regular"] == 1
    assert cap.warnings[0][1]["n"] == 2


def test_scan_non_regular_nodes_missing_root_is_empty(tmp_path: Path) -> None:
    assert private_storage.scan_non_regular_nodes(tmp_path / "absent") == []


def test_scan_non_regular_nodes_does_not_follow_a_symlinked_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    os.mkfifo(target / "queue")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    assert private_storage.scan_non_regular_nodes(link) == []


def test_private_tree_root_problem_matches_converges_refusals(tmp_path: Path) -> None:
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


def test_private_file_problem_sees_a_fifo(tmp_path: Path) -> None:
    path = tmp_path / "marker"
    os.mkfifo(path)
    assert private_storage.private_file_problem(path) == "is not a regular file"
