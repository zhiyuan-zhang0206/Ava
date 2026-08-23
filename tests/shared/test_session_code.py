"""Session-code provenance stays attached to one process identity."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from shared import session_code
from shared.session_record import SessionRecord


def _fake_health_payload(
    name: str, home: str, pid: int, sha: str
) -> Callable[[str], dict[str, str | int]]:
    """A typed stand-in for `session_code._health_payload`."""

    def payload(_url: str) -> dict[str, str | int]:
        return {"name": name, "home": home, "pid": pid, "sha": sha}

    return payload


def _write_session_record(root: Path, *, pid: int, started_at: float) -> None:
    SessionRecord(
        pid=pid,
        create_time=started_at,
        cmd="daemon",
        cwd="/repo",
        started_at=started_at,
        starttime=pid,
    ).write(root / "sessions" / "ava-ops.json")


def test_launch_sha_does_not_survive_a_reused_session_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A healthcheck respawn with the same name must not inherit old code facts."""
    monkeypatch.setattr(session_code, "run_dir", lambda: tmp_path)
    _write_session_record(tmp_path, pid=10, started_at=1.0)

    session_code.record_launch("ava-ops", "oldsha")
    assert session_code.launched_sha("ava-ops") == "oldsha"

    _write_session_record(tmp_path, pid=11, started_at=2.0)
    assert session_code.launched_sha("ava-ops") is None


def test_health_sha_covers_a_session_that_predates_the_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A legacy healthy daemon reports its own frozen SHA on `/healthz`."""
    monkeypatch.setattr(session_code, "run_dir", lambda: tmp_path)
    monkeypatch.setattr(session_code, "ava_home", lambda: tmp_path)
    _write_session_record(tmp_path, pid=10, started_at=1.0)
    monkeypatch.setattr(
        session_code,
        "_health_payload",
        _fake_health_payload("ops", str(tmp_path), 10, "oldsha"),
    )

    assert (
        session_code.launched_sha(
            "ava-ops", service="ops", health_url="http://localhost:8113/healthz"
        )
        == "oldsha"
    )


def test_health_sha_overrides_a_launch_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The live process is more authoritative than a launch-time bookmark."""
    monkeypatch.setattr(session_code, "run_dir", lambda: tmp_path)
    monkeypatch.setattr(session_code, "ava_home", lambda: tmp_path)
    _write_session_record(tmp_path, pid=10, started_at=1.0)
    session_code.record_launch("ava-ops", "newsha")
    monkeypatch.setattr(
        session_code,
        "_health_payload",
        _fake_health_payload("ops", str(tmp_path), 10, "oldsha"),
    )

    assert (
        session_code.launched_sha(
            "ava-ops", service="ops", health_url="http://localhost:8113/healthz"
        )
        == "oldsha"
    )


def test_health_sha_requires_the_session_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A response from another process is unknown, never stale by guess."""
    monkeypatch.setattr(session_code, "run_dir", lambda: tmp_path)
    monkeypatch.setattr(session_code, "ava_home", lambda: tmp_path)
    _write_session_record(tmp_path, pid=10, started_at=1.0)
    monkeypatch.setattr(
        session_code,
        "_health_payload",
        _fake_health_payload("ops", str(tmp_path), 11, "oldsha"),
    )

    assert (
        session_code.launched_sha(
            "ava-ops", service="ops", health_url="http://localhost:8113/healthz"
        )
        is None
    )
