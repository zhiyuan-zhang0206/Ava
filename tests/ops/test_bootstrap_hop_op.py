"""Channel C's hop handlers and channel E's normal-continuation handler -- plus
the detached retained-image entry spawns both reach.

C (`ops.ops_bootstrap_hop`): the handler verifies the payload's request path as
canonical private unit state (absolute, canonical, owned, 0600, regular, inside
`{home}/run`, <=64 KiB), then spawns the detached `ava-updater` session running
the retained candidate image's `--bootstrap-hop` entry. It never reads the
request's content, never pauses and never seeds a handoff -- the child's own
CAS owns the authority checks -- and a non-Linux platform is refused before
anything is spawned. Its read-only recovery-journal face (C-4) sits below.

E (`ops.ops_normal_continue`, tests folded in from
`tests/ops/test_normal_continue_op.py` at the structure budget, task #4129 I6):
the sibling handler verifies the same stat face, then spawns the session on the
retained image's `--normal-release` (step "drive") or `--normal-commit` (step
"commit") entry; the request is never read, nothing pauses, no handoff is
seeded.

Both spawn functions' launch mechanics run with the session seam stubbed, under
the `real_cluster_spawn` opt-out (mirroring `test_cluster_spawn_backend.py`).
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
from pathlib import Path
from typing import Literal

import pytest

from ops import (
    cluster as cluster_facade,
)
from ops import (
    cluster_deploy,
    cluster_session,
    ops_bootstrap_hop,
    ops_normal_continue,
    unit_local,
)
from ops.ops_bootstrap_hop import (
    BootstrapHopPayload,
    BootstrapHopResult,
    BootstrapRecoveryReadPayload,
    BootstrapRecoveryReadResult,
)
from ops.ops_normal_continue import NormalContinuePayload, NormalContinueResult
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


def _request_file(home: Path, name: str = "prepared-hop-request-abc.json") -> Path:
    request = home / "run" / name
    request.write_text("{}\n", encoding="utf-8")
    request.chmod(0o600)
    return request


def _payload(request: Path, *, artifact_digest: str = ARTIFACT) -> BootstrapHopPayload:
    return BootstrapHopPayload(hop_request=str(request), artifact_digest=artifact_digest)


def _as_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ops_bootstrap_hop.sys, "platform", "linux")


class _SpawnRecorder:
    """Records each `spawn_bootstrap_hop` call instead of spawning anything."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []

    def __call__(self, request: Path, *, artifact_digest: str) -> dict[str, str]:
        self.calls.append((request, artifact_digest))
        return {"session": "ava-test-updater", "log": "/unit/logs/updater-1.log"}


def _stub_spawn(monkeypatch: pytest.MonkeyPatch) -> _SpawnRecorder:
    recorder = _SpawnRecorder()
    monkeypatch.setattr(ops_bootstrap_hop, "spawn_bootstrap_hop", recorder)
    return recorder


def _refuse(home: Path, payload: BootstrapHopPayload) -> None:
    del home
    with pytest.raises(ReleaseRejectedError, match="canonical private unit reference"):
        ops_bootstrap_hop.cluster_bootstrap_hop_op(payload)


def _normal_payload(
    request: Path,
    *,
    step: Literal["drive", "commit"] = "drive",
    artifact_digest: str = ARTIFACT,
) -> NormalContinuePayload:
    return NormalContinuePayload(
        continue_request=str(request), step=step, artifact_digest=artifact_digest
    )


class _NormalSpawnRecorder:
    """Records each `spawn_normal_continue` call instead of spawning anything."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, str, str]] = []

    def __call__(self, request: Path, *, step: str, artifact_digest: str) -> dict[str, str]:
        self.calls.append((request, step, artifact_digest))
        return {"session": "ava-test-updater", "log": "/unit/logs/updater-1.log"}


def _stub_normal_spawn(monkeypatch: pytest.MonkeyPatch) -> _NormalSpawnRecorder:
    recorder = _NormalSpawnRecorder()
    monkeypatch.setattr(ops_bootstrap_hop, "spawn_normal_continue", recorder)
    return recorder


def _refuse_normal(payload: NormalContinuePayload) -> None:
    with pytest.raises(ReleaseRejectedError, match="canonical private unit reference"):
        ops_normal_continue.cluster_normal_continue_op(payload)


def test_payload_models_roundtrip_on_the_wire_shape() -> None:
    payload = _payload(Path("/unit/run/request.json"))
    wire = payload.model_dump(mode="json")
    assert BootstrapHopPayload.model_validate_json(json.dumps(wire)) == payload
    with pytest.raises(ValueError):
        BootstrapHopPayload.model_validate_json(json.dumps({**wire, "extra": 1}))
    with pytest.raises(ValueError):
        BootstrapHopPayload(hop_request="", artifact_digest=ARTIFACT)
    with pytest.raises(ValueError):
        BootstrapHopPayload(hop_request="/unit/run/request.json", artifact_digest="not-a-digest")


def test_non_linux_platform_refuses_before_any_spawn(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ops_bootstrap_hop.sys, "platform", "win32")
    recorder = _stub_spawn(monkeypatch)

    with pytest.raises(ReleaseRejectedError, match="no native proof"):
        ops_bootstrap_hop.cluster_bootstrap_hop_op(_payload(_request_file(unit_home)))

    assert recorder.calls == []


def test_missing_request_refuses(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _as_linux(monkeypatch)
    recorder = _stub_spawn(monkeypatch)

    _refuse(unit_home, _payload(unit_home / "run" / "absent.json"))

    assert recorder.calls == []


def test_symlinked_request_refuses(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _as_linux(monkeypatch)
    recorder = _stub_spawn(monkeypatch)
    real = unit_home / "run" / "real.json"
    real.write_text("{}\n", encoding="utf-8")
    real.chmod(0o600)
    link = unit_home / "run" / "link.json"
    link.symlink_to(real)

    _refuse(unit_home, _payload(link))

    assert recorder.calls == []


def test_request_outside_the_run_directory_refuses(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_linux(monkeypatch)
    recorder = _stub_spawn(monkeypatch)
    outside = unit_home / "hop-request.json"
    outside.write_text("{}\n", encoding="utf-8")
    outside.chmod(0o600)

    _refuse(unit_home, _payload(outside))

    assert recorder.calls == []


def test_non_private_mode_refuses(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _as_linux(monkeypatch)
    recorder = _stub_spawn(monkeypatch)
    request = _request_file(unit_home)
    request.chmod(0o644)

    _refuse(unit_home, _payload(request))

    assert recorder.calls == []


def test_directory_request_refuses(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _as_linux(monkeypatch)
    recorder = _stub_spawn(monkeypatch)
    directory = unit_home / "run" / "request.json"
    directory.mkdir()
    directory.chmod(0o600)  # so the S_ISREG refusal has independent teeth (QA N4)

    _refuse(unit_home, _payload(directory))

    assert recorder.calls == []


def test_oversized_request_refuses(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _as_linux(monkeypatch)
    recorder = _stub_spawn(monkeypatch)
    oversized = unit_home / "run" / "oversized.json"
    oversized.write_bytes(b"{" + b" " * (unit_local._MAX_REQUEST_BYTES + 1) + b"}")
    oversized.chmod(0o600)

    _refuse(unit_home, _payload(oversized))

    assert recorder.calls == []


def test_foreign_owner_refuses(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _as_linux(monkeypatch)
    recorder = _stub_spawn(monkeypatch)
    request = _request_file(unit_home)

    real_uid = os.getuid()

    def _foreign_uid() -> int:
        return real_uid + 1

    monkeypatch.setattr(ops_bootstrap_hop.os, "getuid", _foreign_uid)

    _refuse(unit_home, _payload(request))

    assert recorder.calls == []


def test_happy_path_spawns_with_the_verified_request(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_linux(monkeypatch)
    recorder = _stub_spawn(monkeypatch)
    request = _request_file(unit_home)

    result = ops_bootstrap_hop.cluster_bootstrap_hop_op(_payload(request))

    assert result == BootstrapHopResult(
        machine="runner",
        home=str(unit_home),
        session="ava-test-updater",
        log="/unit/logs/updater-1.log",
    )
    assert recorder.calls == [(request, ARTIFACT)]


def test_daemon_dispatch_accepts_the_json_shaped_payload(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The op envelope crosses the wire as JSON, so the dispatch arm must give
    the handler a JSON-mode-validated payload."""
    from services.agent_ops import daemon as ops_daemon

    seen: list[BootstrapHopPayload] = []

    def handler(payload: BootstrapHopPayload) -> BootstrapHopResult:
        seen.append(payload)
        return BootstrapHopResult(
            machine="runner", home=str(unit_home), session="ava-test-updater", log="/x/log"
        )

    monkeypatch.setattr(ops_bootstrap_hop, "cluster_bootstrap_hop_op", handler)
    payload = _payload(_request_file(unit_home))

    status, result = ops_daemon._dispatch_sync(
        "cluster_bootstrap_hop", payload.model_dump(mode="json")
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
def test_spawn_bootstrap_hop_runs_the_candidate_image_entry(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
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

    result = ops_bootstrap_hop.spawn_bootstrap_hop(request, artifact_digest=ARTIFACT)

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
        f"--bootstrap-hop {shlex.quote(str(request))}; rc=$?; " in shell_cmd
    )
    assert "[session-exit] rc=$rc" in shell_cmd
    assert f"2>&1 | tee -a {shlex.quote(result['log'])}" in shell_cmd


@pytest.mark.real_cluster_spawn
def test_spawn_bootstrap_hop_refuses_with_a_live_orchestration_session(
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

    with pytest.raises(cluster_deploy.ClusterUpdateInProgress, match="already exists"):
        ops_bootstrap_hop.spawn_bootstrap_hop(_request_file(unit_home), artifact_digest=ARTIFACT)

    assert spawned == []


@pytest.mark.real_cluster_spawn
def test_spawn_bootstrap_hop_refuses_a_session_inside_the_lock(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session_names(monkeypatch)
    monkeypatch.setattr("shared.ui_update_state.lifecycle_lock", contextlib.nullcontext)

    def _none(name: str) -> bool:
        del name
        return False

    monkeypatch.setattr(cluster_session, "_has_orchestration_session", _none)

    def _live() -> str | None:
        return "ava-test-rollout"

    monkeypatch.setattr(cluster_session, "live_orchestration_session", _live)
    spawned: list[str] = []

    def _capture(session: str, *, shell_cmd: str, native_cmd: str) -> None:
        del shell_cmd, native_cmd
        spawned.append(session)

    monkeypatch.setattr(cluster_session, "_spawn_detached_session", _capture)

    with pytest.raises(cluster_deploy.ClusterUpdateInProgress, match="already exists"):
        ops_bootstrap_hop.spawn_bootstrap_hop(_request_file(unit_home), artifact_digest=ARTIFACT)

    assert spawned == []


@pytest.mark.real_cluster_spawn
def test_spawn_bootstrap_hop_refuses_an_unretained_image(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session_names(monkeypatch)

    def _none(name: str) -> bool:
        del name
        return False

    monkeypatch.setattr(cluster_session, "_has_orchestration_session", _none)
    spawned: list[str] = []

    def _capture(session: str, *, shell_cmd: str, native_cmd: str) -> None:
        del shell_cmd, native_cmd
        spawned.append(session)

    monkeypatch.setattr(cluster_session, "_spawn_detached_session", _capture)

    with pytest.raises(ReleaseRejectedError, match="not retained"):
        ops_bootstrap_hop.spawn_bootstrap_hop(_request_file(unit_home), artifact_digest="9" * 64)

    assert spawned == []


# ─── the read-only recovery-journal face (task #4129 C-4) ─────────────────────


def _recovery_envelope(journal: dict[str, object]) -> dict[str, object]:
    return {"version": 1, "generation": "bootstrap", "journal": journal}


def _bootstrap_journal(stage: str) -> dict[str, object]:
    """The canonical journal shape (mirrors `tests/shared/test_updater_handoff.py`)."""
    return {
        "request": "/unit/run/bootstrap.json",
        "request_digest": "a" * 64,
        "inventory_digest": "b" * 64,
        "candidate_context_digest": "c" * 64,
        "recovery_context_digest": "d" * 64,
        "normal_release_planned": False,
        "stage": stage,
        "cron": "",
        "phases": [
            {
                "stage": stage,
                "observed_at": "2026-09-04T00:00:00Z",
                "monotonic_s": 0.0,
                "pid": 1,
                "elapsed_s": None,
            }
        ],
        "normal_release": None,
    }


def test_recovery_read_wire_shapes() -> None:
    assert BootstrapRecoveryReadPayload().model_dump(mode="json") == {}
    with pytest.raises(ValueError):
        BootstrapRecoveryReadPayload.model_validate_json(json.dumps({"payload": 1}))
    result = BootstrapRecoveryReadResult(
        machine="runner",
        home="/unit",
        journal_present=True,
        journal_stage="candidate_ready",
        normal_release_stage="committed",
    )
    wire = result.model_dump(mode="json")
    assert wire["normal_release_stage"] == "committed"
    assert BootstrapRecoveryReadResult.model_validate_json(json.dumps(wire)) == result
    absent = BootstrapRecoveryReadResult(machine="runner", home="/unit", journal_present=False)
    assert absent.journal_stage is None
    assert absent.normal_release_stage is None


def test_recovery_read_reports_an_absent_journal(unit_home: Path) -> None:
    result = ops_bootstrap_hop.cluster_bootstrap_recovery_read_op(BootstrapRecoveryReadPayload())

    assert result == BootstrapRecoveryReadResult(
        machine="runner", home=str(unit_home), journal_present=False
    )


def test_recovery_read_reports_a_readable_journal_stage(unit_home: Path) -> None:
    journal = _bootstrap_journal("prepared")
    path = unit_home / "run" / "updater-bootstrap-recovery.json"
    path.write_text(json.dumps(_recovery_envelope(journal)), encoding="utf-8")

    result = ops_bootstrap_hop.cluster_bootstrap_recovery_read_op(BootstrapRecoveryReadPayload())

    assert result.journal_present is True
    assert result.journal_stage == "prepared"
    assert result.normal_release_stage is None


def test_recovery_read_reports_the_nested_normal_release_stage(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The nested continuation stage rides along (task #4129 I6): the
    commit-tail wait reads it, so the extraction must not lose it."""
    from shared import updater_handoff

    journal = _bootstrap_journal("candidate_ready")
    journal["normal_release_planned"] = True
    journal["normal_release"] = {"stage": "committed"}
    monkeypatch.setattr(
        updater_handoff,
        "read_bootstrap_recovery",
        lambda: _recovery_envelope(journal),
    )

    result = ops_bootstrap_hop.cluster_bootstrap_recovery_read_op(BootstrapRecoveryReadPayload())

    assert result.journal_present is True
    assert result.journal_stage == "candidate_ready"
    assert result.normal_release_stage == "committed"


def test_recovery_read_reports_a_malformed_journal_as_present(unit_home: Path) -> None:
    path = unit_home / "run" / "updater-bootstrap-recovery.json"
    path.write_text('{"version": 1}', encoding="utf-8")

    result = ops_bootstrap_hop.cluster_bootstrap_recovery_read_op(BootstrapRecoveryReadPayload())

    assert result.journal_present is True
    assert result.journal_stage is None


def test_daemon_dispatch_accepts_the_recovery_read_payload(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The op envelope crosses the wire as JSON; the dispatch arm must give the
    handler a JSON-mode-validated no-argument payload."""
    from services.agent_ops import daemon as ops_daemon

    seen: list[BootstrapRecoveryReadPayload] = []

    def handler(payload: BootstrapRecoveryReadPayload) -> BootstrapRecoveryReadResult:
        seen.append(payload)
        return BootstrapRecoveryReadResult(
            machine="runner", home=str(unit_home), journal_present=False
        )

    monkeypatch.setattr(ops_bootstrap_hop, "cluster_bootstrap_recovery_read_op", handler)

    status, result = ops_daemon._dispatch_sync("cluster_bootstrap_recovery_read", {})

    assert status == "completed"
    assert seen == [BootstrapRecoveryReadPayload()]
    assert result == {
        "machine": "runner",
        "home": str(unit_home),
        "journal_present": False,
        "journal_stage": None,
        "normal_release_stage": None,
    }


def test_unmarked_tests_hold_the_refused_spawn_guard() -> None:
    """The conftest guard must cover the fourth detached-session trigger too.

    This test is unmarked, so reaching the real function here is exactly the
    escape `_guard_cluster_spawn` exists to refuse."""
    with pytest.raises(AssertionError, match="spawn_bootstrap_hop"):
        cluster_facade.spawn_bootstrap_hop(
            Path("/nonexistent/request.json"), artifact_digest=ARTIFACT
        )


# ── channel E: the normal-continuation handler + spawn (task #4129 I6) ───────


def test_normal_continue_payload_models_roundtrip_on_the_wire_shape() -> None:
    payload = _normal_payload(Path("/unit/run/request.json"))
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


def test_normal_continue_refuses_on_a_non_linux_platform_before_any_spawn(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ops_normal_continue.sys, "platform", "win32")
    recorder = _stub_normal_spawn(monkeypatch)

    with pytest.raises(ReleaseRejectedError, match="no native proof"):
        ops_normal_continue.cluster_normal_continue_op(_normal_payload(_request_file(unit_home)))

    assert recorder.calls == []


def test_normal_continue_missing_request_refuses(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_linux(monkeypatch)
    recorder = _stub_normal_spawn(monkeypatch)

    _refuse_normal(_normal_payload(unit_home / "run" / "absent.json"))

    assert recorder.calls == []


def test_normal_continue_non_private_mode_refuses(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_linux(monkeypatch)
    recorder = _stub_normal_spawn(monkeypatch)
    request = _request_file(unit_home)
    request.chmod(0o644)

    _refuse_normal(_normal_payload(request))

    assert recorder.calls == []


def test_normal_continue_oversized_request_refuses(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_linux(monkeypatch)
    recorder = _stub_normal_spawn(monkeypatch)
    oversized = unit_home / "run" / "oversized.json"
    oversized.write_bytes(b"{" + b" " * (unit_local._MAX_REQUEST_BYTES + 1) + b"}")
    oversized.chmod(0o600)

    _refuse_normal(_normal_payload(oversized))

    assert recorder.calls == []


def test_happy_path_drive_passes_the_verified_request(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_linux(monkeypatch)
    recorder = _stub_normal_spawn(monkeypatch)
    request = _request_file(unit_home)

    result = ops_normal_continue.cluster_normal_continue_op(_normal_payload(request, step="drive"))

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
    recorder = _stub_normal_spawn(monkeypatch)
    request = _request_file(unit_home)

    ops_normal_continue.cluster_normal_continue_op(_normal_payload(request, step="commit"))

    assert recorder.calls == [(request, "commit", ARTIFACT)]


def test_daemon_dispatch_accepts_the_normal_continue_payload(
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
    payload = _normal_payload(_request_file(unit_home), step="commit")

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

    result = ops_bootstrap_hop.spawn_normal_continue(request, step=step, artifact_digest=ARTIFACT)

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

    with pytest.raises(cluster_deploy.ClusterUpdateInProgress, match="already exists"):
        ops_bootstrap_hop.spawn_normal_continue(
            _request_file(unit_home), step="drive", artifact_digest=ARTIFACT
        )

    assert spawned == []


@pytest.mark.real_cluster_spawn
def test_spawn_normal_continue_rejects_an_unknown_step(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session_names(monkeypatch)

    with pytest.raises(ValueError, match="unknown normal-continuation step"):
        ops_bootstrap_hop.spawn_normal_continue(
            _request_file(unit_home), step="walk", artifact_digest=ARTIFACT
        )


def test_unmarked_tests_hold_the_refused_normal_continue_spawn_guard() -> None:
    """The conftest guard must cover the fifth detached-session trigger too.

    This test is unmarked, so reaching the real function here is exactly the
    escape `_guard_cluster_spawn` exists to refuse."""
    with pytest.raises(AssertionError, match="spawn_normal_continue"):
        cluster_facade.spawn_normal_continue(
            Path("/nonexistent/request.json"), step="drive", artifact_digest=ARTIFACT
        )
