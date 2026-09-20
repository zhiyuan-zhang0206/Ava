"""`ops.ops_normal_continue` + `ops.updater_entries.spawn_normal_continue` -- channel E.

The handler verifies the payload's request path as canonical private unit state
(the shared `ops.unit_local.private_unit_reference` stat face), then spawns the
detached `ava-updater` session running the retained candidate image's
`--normal-release` (step "drive") or `--normal-commit` (step "commit") entry. It
never reads the request's content, never pauses and never seeds a handoff -- the
child re-derives every binding itself -- and a non-Linux platform is refused
before anything is spawned.

The spawn function's launch mechanics run with the session seam stubbed, under
the `real_cluster_spawn` opt-out (mirroring `test_bootstrap_hop_op.py`).
"""

from __future__ import annotations

import contextlib
import json
import shlex
from pathlib import Path
from typing import Literal

import pytest

from ops import cluster as cluster_facade
from ops import cluster_session, ops_normal_continue, unit_local, updater_entries
from ops.rpc_normal_continue import NormalContinuePayload, NormalContinueResult
from shared.config import settings
from shared.runtime_release import ReleaseRejectedError

ARTIFACT = "a" * 64


@pytest.fixture
def unit_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """One canonical unit home with a retained candidate image and identity."""
    home = (tmp_path / "unit").resolve()
    interpreter = home / "releases" / ARTIFACT / "venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    (home / "machine_name").write_text("runner\n", encoding="utf-8")
    (home / "run").mkdir()
    monkeypatch.setattr(settings.general, "ava_home", home)
    return home


def _request_file(home: Path) -> Path:
    request = home / "run" / "prepared-normal-request-abc.json"
    request.write_text("{}\n", encoding="utf-8")
    request.chmod(0o600)
    return request


def _payload(
    request: Path,
    *,
    step: Literal["drive", "commit"] = "drive",
    artifact_digest: str = ARTIFACT,
) -> NormalContinuePayload:
    return NormalContinuePayload(
        continue_request=str(request), step=step, artifact_digest=artifact_digest
    )


def _as_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ops_normal_continue.sys, "platform", "linux")


class _SpawnRecorder:
    """Records each `spawn_normal_continue` call instead of spawning anything."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, str, str]] = []

    def __call__(self, request: Path, *, step: str, artifact_digest: str) -> dict[str, str]:
        self.calls.append((request, step, artifact_digest))
        return {"session": "ava-test-updater", "log": "/unit/logs/updater-1.log"}


def _stub_spawn(monkeypatch: pytest.MonkeyPatch) -> _SpawnRecorder:
    recorder = _SpawnRecorder()
    monkeypatch.setattr(updater_entries, "spawn_normal_continue", recorder)
    return recorder


def _refuse(payload: NormalContinuePayload) -> None:
    with pytest.raises(ReleaseRejectedError, match="canonical private unit reference"):
        ops_normal_continue.cluster_normal_continue_op(payload)


def test_payload_models_roundtrip_on_the_wire_shape() -> None:
    payload = _payload(Path("/unit/run/request.json"))
    wire = payload.model_dump(mode="json")
    assert NormalContinuePayload.model_validate_json(json.dumps(wire)) == payload
    with pytest.raises(ValueError):
        NormalContinuePayload.model_validate_json(json.dumps({**wire, "extra": 1}))
    with pytest.raises(ValueError):
        NormalContinuePayload(continue_request="", step="drive", artifact_digest=ARTIFACT)
    with pytest.raises(ValueError):
        NormalContinuePayload.model_validate(
            {
                "continue_request": "/unit/run/request.json",
                "step": "walk",
                "artifact_digest": ARTIFACT,
            }
        )
    with pytest.raises(ValueError):
        NormalContinuePayload(
            continue_request="/unit/run/request.json",
            step="drive",
            artifact_digest="not-a-digest",
        )


def test_non_linux_platform_refuses_before_any_spawn(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ops_normal_continue.sys, "platform", "win32")
    recorder = _stub_spawn(monkeypatch)

    with pytest.raises(ReleaseRejectedError, match="no native proof"):
        ops_normal_continue.cluster_normal_continue_op(_payload(_request_file(unit_home)))

    assert recorder.calls == []


def test_missing_request_refuses(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _as_linux(monkeypatch)
    recorder = _stub_spawn(monkeypatch)

    _refuse(_payload(unit_home / "run" / "absent.json"))

    assert recorder.calls == []


def test_non_private_mode_refuses(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _as_linux(monkeypatch)
    recorder = _stub_spawn(monkeypatch)
    request = _request_file(unit_home)
    request.chmod(0o644)

    _refuse(_payload(request))

    assert recorder.calls == []


def test_oversized_request_refuses(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _as_linux(monkeypatch)
    recorder = _stub_spawn(monkeypatch)
    oversized = unit_home / "run" / "oversized.json"
    oversized.write_bytes(b"{" + b" " * (unit_local._MAX_REQUEST_BYTES + 1) + b"}")
    oversized.chmod(0o600)

    _refuse(_payload(oversized))

    assert recorder.calls == []


def test_happy_path_drive_passes_the_verified_request(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_linux(monkeypatch)
    recorder = _stub_spawn(monkeypatch)
    request = _request_file(unit_home)

    result = ops_normal_continue.cluster_normal_continue_op(_payload(request, step="drive"))

    assert result == NormalContinueResult(
        machine="runner",
        home=str(unit_home),
        session="ava-test-updater",
        log="/unit/logs/updater-1.log",
    )
    assert recorder.calls == [(request, "drive", ARTIFACT)]


def test_happy_path_commit_passes_the_step_through(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_linux(monkeypatch)
    recorder = _stub_spawn(monkeypatch)
    request = _request_file(unit_home)

    ops_normal_continue.cluster_normal_continue_op(_payload(request, step="commit"))

    assert recorder.calls == [(request, "commit", ARTIFACT)]


def test_daemon_dispatch_accepts_the_json_shaped_payload(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The op envelope crosses the wire as JSON, so the dispatch arm must give
    the handler a JSON-mode-validated payload."""
    from services.agent_ops import daemon as ops_daemon

    seen: list[NormalContinuePayload] = []

    def handler(payload: NormalContinuePayload) -> NormalContinueResult:
        seen.append(payload)
        return NormalContinueResult(
            machine="runner", home=str(unit_home), session="ava-test-updater", log="/x/log"
        )

    monkeypatch.setattr(ops_normal_continue, "cluster_normal_continue_op", handler)
    payload = _payload(_request_file(unit_home), step="commit")

    status, result = ops_daemon._dispatch_sync(
        "cluster_normal_continue", payload.model_dump(mode="json")
    )

    assert status == "completed"
    assert seen == [payload]
    assert result == {
        "machine": "runner",
        "home": str(unit_home),
        "session": "ava-test-updater",
        "log": "/x/log",
    }


def _session_names(monkeypatch: pytest.MonkeyPatch) -> None:
    def _name(service: str) -> str:
        return f"ava-test-{service}"

    monkeypatch.setattr("shared.cluster.session_name", _name)


@pytest.mark.real_cluster_spawn
@pytest.mark.parametrize(
    ("step", "flag"),
    [("drive", "--normal-release"), ("commit", "--normal-commit")],
)
def test_spawn_normal_continue_runs_the_candidate_image_entry(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch, step: str, flag: str
) -> None:
    _session_names(monkeypatch)
    monkeypatch.setattr("shared.ui_update_state.lifecycle_lock", contextlib.nullcontext)

    def _no_live_session() -> str | None:
        return None

    monkeypatch.setattr(cluster_session, "live_orchestration_session", _no_live_session)
    spawned: list[tuple[str, str, str]] = []

    def _capture(session: str, *, shell_cmd: str, native_cmd: str) -> None:
        spawned.append((session, shell_cmd, native_cmd))

    monkeypatch.setattr(cluster_session, "_spawn_detached_session", _capture)
    request = _request_file(unit_home)

    result = updater_entries.spawn_normal_continue(request, step=step, artifact_digest=ARTIFACT)

    assert result["session"] == "ava-test-updater"
    assert Path(result["log"]).parent == unit_home / "logs"
    assert Path(result["log"]).name.startswith("updater-")
    assert len(spawned) == 1
    session, shell_cmd, native_cmd = spawned[0]
    assert session == "ava-test-updater"
    assert shell_cmd == native_cmd
    interpreter = str(unit_home / "releases" / ARTIFACT / "venv" / "bin" / "python")
    assert f"{{ export AVA_CLI_LOG_NAME=updater; if cd {shlex.quote(str(unit_home))}; " in shell_cmd
    assert (
        f"then {shlex.quote(interpreter)} -I -B -m cli.commands._update_agent_runner "
        f"{flag} {shlex.quote(str(request))}; rc=$?; " in shell_cmd
    )
    assert "[session-exit] rc=$rc" in shell_cmd
    assert f"2>&1 | tee -a {shlex.quote(result['log'])}" in shell_cmd


@pytest.mark.real_cluster_spawn
def test_spawn_normal_continue_refuses_with_a_live_orchestration_session(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session_names(monkeypatch)

    def _alive(name: str) -> bool:
        return name == "ava-test-updater"

    monkeypatch.setattr(cluster_session, "_has_orchestration_session", _alive)
    spawned: list[str] = []

    def _capture(session: str, *, shell_cmd: str, native_cmd: str) -> None:
        del shell_cmd, native_cmd
        spawned.append(session)

    monkeypatch.setattr(cluster_session, "_spawn_detached_session", _capture)

    with pytest.raises(updater_entries.ClusterUpdateInProgress, match="already exists"):
        updater_entries.spawn_normal_continue(
            _request_file(unit_home), step="drive", artifact_digest=ARTIFACT
        )

    assert spawned == []


@pytest.mark.real_cluster_spawn
def test_spawn_normal_continue_rejects_an_unknown_step(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session_names(monkeypatch)

    with pytest.raises(ValueError, match="unknown normal-continuation step"):
        updater_entries.spawn_normal_continue(
            _request_file(unit_home), step="walk", artifact_digest=ARTIFACT
        )


def test_unmarked_tests_hold_the_refused_spawn_guard() -> None:
    """The conftest guard must cover the fifth detached-session trigger too.

    This test is unmarked, so reaching the real function here is exactly the
    escape `_guard_cluster_spawn` exists to refuse."""
    with pytest.raises(AssertionError, match="spawn_normal_continue"):
        cluster_facade.spawn_normal_continue(
            Path("/nonexistent/request.json"), step="drive", artifact_digest=ARTIFACT
        )
