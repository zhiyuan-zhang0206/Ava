"""Observable contracts of the writers being moved to shared.atomic_io."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from cli.commands import _grafana_render, _observatory_urls, _otel_collector
from ops import pty_close_notices
from shared import (
    atomic_io,
    coding_session_owner_record,
    delivery_outbox,
    editable_install,
    pause_owner,
    spawn_receipt,
    start_serving,
    ui_update_state,
)
from shared import updater_handoff as handoff
from shared.sessions.pty import allocation_freeze
from tests.shared.test_updater_handoff import (
    _isolated as _isolated,
)
from tests.shared.test_updater_handoff import (
    _isolated_attempts as _isolated_attempts,
)
from tests.shared.test_updater_handoff import (
    _retained_bootstrap,
    _write_normal_through,
)


def _marker_write(case: str, path: Path) -> None:
    payload: dict[str, object] = {"z": 1, "a": "value"}
    if case == "start":
        start_serving._write_state("starting", "generation")
    elif case == "receipt":
        spawn_receipt._write_atomic_text(path, '{"z":1,"a":"value"}')
    elif case == "owner":
        key = coding_session_owner_record.CodingSessionKey("cluster", "workspace", "codex")
        coding_session_owner_record.write_unlocked(
            coding_session_owner_record.CodingSessionOwner(key=key, status="inactive")
        )
    elif case == "pause":
        pause_owner._write_atomic(path, payload)
    elif case == "freeze":
        allocation_freeze._write_atomic(path, payload)
    elif case == "ui":
        ui_update_state._write_atomic(path, payload)
    else:
        raise AssertionError(case)


@pytest.mark.parametrize("case", ["start", "receipt", "owner", "pause", "freeze", "ui"])
def test_marker_commit_survives_directory_sync_failure_and_cleans_temps(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    modules = {
        "start": start_serving,
        "receipt": spawn_receipt,
        "owner": coding_session_owner_record,
        "pause": pause_owner,
        "freeze": allocation_freeze,
        "ui": ui_update_state,
    }
    module = modules[case]
    path = tmp_path / "state.json"
    path.write_bytes(b"old")
    if case in ("start", "owner"):

        def state_path(*_args: object) -> Path:
            return path

        monkeypatch.setattr(module, "state_path", state_path)

    def fail_sync(_path: Path) -> None:
        raise OSError("directory sync failure")

    monkeypatch.setattr(module, "_fsync_parent", fail_sync)
    _marker_write(case, path)
    raw = path.read_bytes()
    if case == "start":
        assert raw == b'{"generation":"generation","schema_version":1,"state":"starting"}'
    elif case == "receipt":
        assert raw == b'{"z":1,"a":"value"}'
    elif case == "owner":
        assert isinstance(json.loads(raw), dict)
    else:
        assert raw == b'{"a":"value","z":1}'
    assert sorted(tmp_path.iterdir()) == [path]
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("case", ["start", "receipt", "owner", "pause", "freeze", "ui"])
def test_marker_replace_failure_keeps_old_content_and_cleans_temps(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"old")
    if case == "start":
        monkeypatch.setattr(start_serving, "state_path", lambda: path)
    elif case == "owner":

        def owner_path(_key: coding_session_owner_record.CodingSessionKey) -> Path:
            return path

        monkeypatch.setattr(coding_session_owner_record, "state_path", owner_path)

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        _marker_write(case, path)
    assert path.read_bytes() == b"old"
    assert sorted(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("writer", [_observatory_urls._atomic_write, _otel_collector._atomic_write])
def test_cli_writer_commits_utf8_without_directory_sync(
    writer: Callable[[Path, str], None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yml"
    path.write_bytes(b"old")
    replaced: list[Path] = []
    original_replace = Path.replace

    def record_replace(source: Path, target: Path) -> Path:
        replaced.append(source)
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", record_replace)
    original_fsync = os.fsync

    def fsync_file_only(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("unexpected directory sync")
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync_file_only)
    writer(path, "café")
    assert path.read_bytes() == "café".encode()
    assert len(replaced) == 1
    assert sorted(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("case", ["outbox", "close-notice"])
@pytest.mark.skipif(os.name == "nt", reason="journals skip parent fsync on Windows")
def test_journal_directory_sync_failure_raises_after_visible_commit(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "record.json"
    original_fsync = os.fsync

    def fail_directory_sync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory sync failure")
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_sync)
    if case == "outbox":
        entry = delivery_outbox.OutboxEntry(
            schema_version=1,
            agent_id=1,
            source="test",
            content="message",
            client_message_id="key",
            created_at="2026-09-24T00:00:00+00:00",
            last_attempt_at="2026-09-24T00:00:00+00:00",
            attempts=1,
            origin_agent_id=None,
            origin_pid=None,
            flush_attempts=0,
            last_flush_at=None,
            state="pending",
            abandon_reason=None,
            abandon_detail=None,
            abandoned_at=None,
        )

        def write() -> None:
            delivery_outbox._write_atomic(path, entry)

        expected = entry.as_dict()
    else:
        notice = pty_close_notices.ClosureNotice(
            machine="host",
            agent_id=1,
            session_id=2,
            name="ava-agent-1-shell-2-test",
            shell_pid=42,
            shell_birth="birth",
            operation="test",
            acquired_at="2026-09-24T00:00:00+00:00",
            reason="test",
            closed_at="2026-09-24T00:00:01+00:00",
        )
        monkeypatch.setattr(pty_close_notices, "journal_dir", lambda: tmp_path)
        path = pty_close_notices._record_path(notice)

        def write() -> None:
            pty_close_notices._write_atomic(notice)

        expected = notice.as_dict()

    with pytest.raises(OSError, match="directory sync failure"):
        write()
    assert path.read_bytes() == json.dumps(expected, separators=(",", ":"), sort_keys=True).encode()
    assert sorted(tmp_path.iterdir()) == [path]


def test_concurrent_marker_writers_publish_complete_snapshots(tmp_path: Path) -> None:
    path = tmp_path / "state.json"

    def write(index: int) -> None:
        pause_owner._write_atomic(path, {"index": index, "data": "x" * 4096})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(24)))
    result = json.loads(path.read_bytes())
    assert result in ({"index": index, "data": "x" * 4096} for index in range(24))
    assert sorted(tmp_path.iterdir()) == [path]


@pytest.mark.skipif(os.name == "nt", reason="Windows does not support directory fsync")
def test_bytes_helper_syncs_file_then_parent_and_sets_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bytes.bin"
    synced: list[str] = []
    original_fsync = os.fsync

    def record_sync(fd: int) -> None:
        synced.append("parent" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", record_sync)
    atomic_io.write_bytes_atomic(path, b"\x00payload", mode=0o600, sync_parent=True)
    assert path.read_bytes() == b"\x00payload"
    assert synced == ["file", "parent"]
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert sorted(tmp_path.iterdir()) == [path]


def test_bytes_helper_can_skip_sync_without_skipping_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bytes.bin"
    path.write_bytes(b"old")

    def fail_sync(_fd: int) -> None:
        pytest.fail("unexpected fsync")

    monkeypatch.setattr(os, "fsync", fail_sync)
    atomic_io.write_bytes_atomic(path, b"new", sync_file=False)
    assert path.read_bytes() == b"new"
    assert sorted(tmp_path.iterdir()) == [path]


@pytest.mark.skipif(os.name == "nt", reason="fchmod mode contract is POSIX-only")
def test_text_helper_applies_requested_public_mode(tmp_path: Path) -> None:
    path = tmp_path / "pointer.pth"
    atomic_io.write_text_atomic(path, "target", mode=0o644, sync_file=False)
    assert path.read_bytes() == b"target"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


@pytest.mark.parametrize(
    "writer", [editable_install._atomic_write_text, _grafana_render._atomic_write]
)
def test_visible_text_writers_use_distinct_sibling_temps_under_concurrency(
    writer: Callable[[Path, str], None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.txt"
    barrier = Barrier(2)
    sources: list[Path] = []
    original_replace = Path.replace

    def rendezvous(source: Path, target: Path) -> Path:
        sources.append(source)
        barrier.wait(timeout=5)
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", rendezvous)
    values = ["first" * 4096, "second" * 4096]

    def write(value: str) -> None:
        writer(path, value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, values))

    assert path.read_text() in values
    assert len(set(sources)) == 2
    assert all(source.parent == path.parent for source in sources)
    assert all(
        source.name.startswith(f".{path.name}.") and source.suffix == ".tmp" for source in sources
    )
    assert sorted(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "writer", [editable_install._atomic_write_text, _grafana_render._atomic_write]
)
def test_visible_text_writers_clean_temp_after_failed_replace(
    writer: Callable[[Path, str], None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.txt"
    path.write_text("old")

    def fail_replace(_source: Path, _target: Path) -> Path:
        raise OSError("replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        writer(path, "new")
    assert path.read_text() == "old"
    assert sorted(tmp_path.iterdir()) == [path]


@pytest.mark.skipif(os.name == "nt", reason="fchmod mode contract is POSIX-only")
@pytest.mark.parametrize(
    "writer", [editable_install._atomic_write_text, _grafana_render._atomic_write]
)
def test_visible_text_writers_set_public_mode_without_sync(
    writer: Callable[[Path, str], None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.txt"

    def unexpected_sync(_fd: int) -> None:
        pytest.fail("visible writer must not fsync")

    monkeypatch.setattr(os, "fsync", unexpected_sync)
    old_umask = os.umask(0o077)
    try:
        writer(path, "café")
    finally:
        os.umask(old_umask)
    assert path.read_bytes() == "café".encode()
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not Windows ACLs")
def test_atomic_pointer_replacement_restores_read_only_mode(tmp_path: Path) -> None:
    path = tmp_path / "pointer.pth"
    path.write_text("old")
    path.chmod(0o444)

    with editable_install._write_window((path,)):
        editable_install._atomic_write_text(path, "new")
        assert stat.S_IMODE(path.stat().st_mode) == 0o644

    assert path.read_text() == "new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o444


# ── the #4117 S5 flip: INJ-14 half-completed unlink (relocated under the 800-line ceiling) ──


def test_clear_completes_across_a_half_completed_unlink() -> None:
    """INJ-14: a crash between the two unlinks must not strand the state file."""
    _retained_bootstrap("candidate_ready", normal_release_planned=True)
    _write_normal_through("committed")
    handoff.bootstrap_state_path().unlink()
    assert handoff.clear("bootstrap")
    assert not handoff.state_path().exists()
    assert not handoff.clear("bootstrap")
