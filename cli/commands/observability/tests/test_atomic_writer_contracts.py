from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from base.deploy.lifecycle import home_lifecycle_locks, start_serving
from base.deploy.maintenance import pause_owner
from base.sessions import coding_session_owner_record
from base.sessions.coding_session_owner_record import CodingSessionStatus
from base.sessions.pty import allocation_freeze
from cli.commands.observability import grafana_render, observatory_urls, otel_collector

_START_GENERATION = "00000000-0000-4000-8000-000000000001"


def _marker_write(case: str, path: Path) -> None:
    payload: dict[str, object] = {"z": 1, "a": "value"}
    if case == "start":
        start_serving.write_state(
            start_serving.ServingState(state="starting", generation=_START_GENERATION)
        )
    elif case == "owner":
        key = coding_session_owner_record.CodingSessionKey("cluster", "workspace", "codex")
        coding_session_owner_record.write_unlocked(
            coding_session_owner_record.CodingSessionOwner(
                key=key,
                status=CodingSessionStatus.LAUNCHING,
                generation="11111111-2222-4333-8444-555555555555",
            )
        )
    elif case == "pause":
        pause_owner.write_atomic(path, payload)
    elif case == "freeze":
        allocation_freeze.write_atomic(path, payload)
    elif case == "locks":
        home_lifecycle_locks.write_atomic(path, payload)
    else:
        raise AssertionError(case)


@pytest.mark.parametrize("case", ["start", "owner", "pause", "freeze", "locks"])
def test_marker_commit_survives_directory_sync_failure_and_cleans_temps(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    modules = {
        "start": start_serving,
        "owner": coding_session_owner_record,
        "pause": pause_owner,
        "freeze": allocation_freeze,
        "locks": home_lifecycle_locks,
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
        assert raw == (
            b'{"schema_version":2,"state":"starting","generation":"'
            + _START_GENERATION.encode()
            + b'","birth":null}'
        )
    elif case == "owner":
        assert isinstance(json.loads(raw), dict)
    else:
        assert raw == b'{"a":"value","z":1}'
    assert sorted(tmp_path.iterdir()) == [path]
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("writer", [observatory_urls.atomic_write, otel_collector.atomic_write])
def test_cli_writer_commits_utf8(writer: Callable[[Path, str], None], tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_bytes(b"old")
    writer(path, "café")
    assert path.read_bytes() == "café".encode()
    assert sorted(tmp_path.iterdir()) == [path]


def test_concurrent_marker_writers_publish_complete_snapshots(tmp_path: Path) -> None:
    path = tmp_path / "state.json"

    def write(index: int) -> None:
        pause_owner.write_atomic(path, {"index": index, "data": "x" * 4096})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(24)))
    result = json.loads(path.read_bytes())
    assert result in ({"index": index, "data": "x" * 4096} for index in range(24))
    assert sorted(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("writer", [grafana_render.atomic_write])
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
