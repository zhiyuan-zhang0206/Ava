"""Update orchestration and gateway posting commands; split from tests/cli/test_commands.py (task #4554)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from pathlib import Path

import pytest

from cli import commands as _cli
from cli.commands import _update_uv_sync
from shared.config import settings
from tests.cli._commands_helpers import _fake_session_backends as _fake_session_backends
from tests.cli._commands_helpers import _FakeResult, _FakeSessionBackend, _git_aware, _sess
from tests.cli._commands_helpers import _hermetic_gateway_base as _hermetic_gateway_base
from tests.cli._commands_helpers import _noop_start_prechecks as _noop_start_prechecks

# ─── multi-machine update orchestration (PR-A) ───────────────────────────────


def test_update_posts_rollout_to_gateway_from_any_host(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ava cluster update` POSTs /api/cluster/rollout to the gateway from ANY
    host — no machine_role() branch (user ruling 2026-08-21, issue #216). The
    body carries origin (default cli:<machine>), mode, force; the response's
    session/log are printed for polling."""
    from typing import cast

    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    calls: list[tuple[str, dict[str, object]]] = []

    class _Resp:
        status_code = 202

        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, object]:
            return {"session": "ava-rollout", "log": "/var/log/u.log", "backend_changed": True}

    def _fake_post(url: str, **_kw: object) -> _Resp:
        calls.append((url, cast(dict[str, object], _kw.get("json"))))
        return _Resp()

    monkeypatch.setattr("httpx.post", _fake_post)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_update()
    assert rc == 0
    assert calls[0][0] == "http://gw:8000/api/cluster/rollout"
    body = calls[0][1]
    assert cast(str, body["origin"]).startswith("cli:")
    assert cast(str, body["mode"]) == "smooth"
    assert body["force"] is False
    assert "ava-rollout" in capsys.readouterr().out


def test_update_restart_only_posts_restart_endpoint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ava cluster update --restart-only` POSTs /api/cluster/restart (bounce
    on current code) — also from any host, no role branch."""
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    calls: list[str] = []

    class _Resp:
        status_code = 202

        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, object]:
            return {"session": "ava-rollout", "log": "/var/log/u.log"}

    def _fake_post(url: str, **_kw: object) -> _Resp:
        calls.append(url)
        return _Resp()

    monkeypatch.setattr("httpx.post", _fake_post)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_update(restart_only=True)
    assert rc == 0
    assert calls == ["http://gw:8000/api/cluster/restart"]
    assert "ava-rollout" in capsys.readouterr().out


def test_gateway_local_update_starts_in_fresh_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`_run_gateway_local_update` runs start as a FRESH `ava` subprocess
    (not in-process cmd_start, which mixes stale pre-pull modules with freshly-
    imported ones and crashes on a large jump). The fresh `ava start` applies
    pending migrations itself early in boot — there is no separate migrate step.
    Order: stop -> force-checkout target_sha -> uv sync -> grafana provisioning
    sync (new-tree venv subprocess) -> `ava start`."""
    from cli.commands import update as _up

    repo = tmp_path
    calls: list[str] = []
    cmds: list[list] = []

    monkeypatch.setattr(_up, "_do_stop", lambda *_a, **_kw: calls.append("stop") or 0)  # pyright: ignore[reportUnknownArgumentType]

    # The orchestration created the recovery anchor before entering the local leg.
    monkeypatch.setattr(_up, "git_checkout_sha", lambda _sha: calls.append("checkout") or "aaaaaaa")  # pyright: ignore[reportUnknownArgumentType]

    def _resolve_identity(ref: str, *, context: str) -> str:
        return ref

    monkeypatch.setattr("cli.commands._update_git.resolve_commit", _resolve_identity)

    def _no_inprocess_migrate():
        raise AssertionError("apply_pending_migrations ran in-process")

    monkeypatch.setattr(_up, "apply_pending_migrations", _no_inprocess_migrate)
    # `cmd_start` is no longer imported into the update module — the start runs
    # as a fresh `ava` subprocess, asserted via the subprocess sequence below.
    # The uv sync itself runs through the production sync seam (run_uv_sync ->
    # run_bounded), not subprocess.run, so it is recorded separately.

    def _fake_sync(_repo: Path, *, timeout_s: float = 600.0) -> _FakeResult:
        calls.append("uv-sync")
        return _FakeResult(returncode=0)

    def _passing_import_gate(
        _repo: Path,
        *,
        allowed_roots: Iterable[Path] = (),
    ) -> tuple[str, ...]:
        return ()

    def _fake_run(cmd, *_a, **_kw):
        cmds.append(list(cmd))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        return _FakeResult(returncode=0)

    monkeypatch.setattr(_update_uv_sync, "run_uv_sync", _fake_sync)
    monkeypatch.setattr(_update_uv_sync, "editable_import_gate", _passing_import_gate)
    monkeypatch.setattr(_up.subprocess, "run", _fake_run)  # pyright: ignore[reportUnknownArgumentType]

    rc = _up._run_gateway_local_update(
        repo,
        target_sha="bbbbbbb",
        pull_recover=("aaaaaaa", {"00000000T000000_baseline"}, None),
    )
    assert rc == 0
    assert calls == ["stop", "checkout", "uv-sync"]
    # uv sync runs through the production sync seam (run_uv_sync, recorded above),
    # then the fresh `ava start` via subprocess.run (the start no longer needs a
    # pty — the session PATH is forwarded authoritatively, so a plain
    # subprocess.run from the detached rollout works). --persist-services keeps this
    # internal restart from rewriting the operator's durable --disable-service marker.
    # Admission stays held until the orchestration unpauses this host, so agents
    # cannot resume before gateway readiness and Phase B.
    # --no-readiness-gate: this leg's readiness question is answered at step 6.5 by the
    # off-box gateway gate, so the child must not also gate (and must not send a slow
    # non-gateway service into _recover_rc's rollback). See
    # tests/cli/test_start_readiness_gate.py.
    # Repo-native skills refresh on the just-landed tree (issue #1289), also as a
    # fresh subprocess so it runs the new revision's update table.
    assert cmds[0][0].endswith(".venv/bin/ava")  # pyright: ignore[reportUnknownMemberType]
    assert cmds[0][1:] == ["skill", "update"]
    # No Grafana provisioning sync step: the LGTM Grafana container mounts
    # deploy/lgtm/config/grafana/provisioning straight from the checkout,
    # so the checkout above already refreshed it.
    assert cmds[1][0].endswith(".venv/bin/ava")  # pyright: ignore[reportUnknownMemberType]
    assert cmds[1][1:] == [
        "start",
        "--persist-services",
        "--no-readiness-gate",
    ]


def test_update_local_runs_in_process_orchestration(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ava cluster update --local` — the explicit escape hatch the detached
    ava-rollout session runs — dispatches to `_run_gateway_orchestration` in
    this foreground process. No role read: the user asked for the local leg on
    whatever host they are on."""
    from cli.commands import update as _up_mod

    monkeypatch.setattr(_up_mod, "_repo_root", lambda: Path("/repo"))
    monkeypatch.setattr(_up_mod, "ava_home", lambda: Path("/home"))
    monkeypatch.setattr(_up_mod, "get_record", lambda _home: None)  # pyright: ignore[reportUnknownArgumentType]

    def _no_post(*_a: object, **_kw: object) -> None:
        raise AssertionError("--local must not POST the gateway")

    monkeypatch.setattr("httpx.post", _no_post)  # pyright: ignore[reportUnknownArgumentType]
    calls: list[str] = []

    def _orch(_repo, **_kw):
        calls.append("orchestration")
        return 0

    monkeypatch.setattr(_cli, "_run_gateway_orchestration", _orch)  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli.cmd_update(local=True)
    assert rc == 0
    assert calls == ["orchestration"]


def test_cmd_start_returns_this_host_to_idle_posture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`ava start` at the end writes the idle posture row — if a previous update
    left the host paused, a manual ava start recovers (no need to ssh + rm). The
    old cluster_paused file was retired with the old-signal sweep (PR5)."""
    calls: list[str] = []
    monkeypatch.setattr("shared.host_deploy_state.set_posture", calls.append)
    monkeypatch.setattr(
        _cli.subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _cli.cmd_start()
    assert rc == 0
    assert calls and calls[-1] == "idle"


def test_cmd_start_finalizes_a_paused_deploy_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression (2026-08-26): a Phase-B `ava start` restores posture without a
    cluster/resume op, so the pause-owner journal must be finalized (paused ->
    resumed, generation preserved) by the start itself — otherwise it stays
    `paused` forever while the host serves (rollout rc=0, deploy-pause-owner.json
    still paused). The exact journaled generation must be kept, so a delayed
    resume for that generation stays an idempotent no-op and a foreign one is
    refused."""
    from datetime import UTC, datetime

    from shared import pause_owner

    owner_path = tmp_path / "deploy-pause-owner.json"
    lock_path = tmp_path / "deploy-pause-owner.lock"
    monkeypatch.setattr(pause_owner, "state_path", lambda: owner_path)
    monkeypatch.setattr(pause_owner, "lock_path", lambda: lock_path)
    monkeypatch.setattr(
        "shared.host_deploy_state.set_posture",
        lambda _p: None,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _cli.subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )

    acquired = datetime(2026, 8, 26, 14, 14, 42, tzinfo=UTC)
    pause_owner.mark_paused("macmini:pid65276", acquired)

    def ready(*_args: object, **_kwargs: object) -> _cli.ReadinessWait:
        assert pause_owner.read().status == "paused", "finalize must wait for readiness"
        return _cli.ReadinessWait((), 0.0, sessions_gone=False)

    monkeypatch.setattr(_cli, "_wait_for_services_ready", ready)
    assert _cli.cmd_start() == 0

    snapshot = pause_owner.read()
    assert snapshot.status == "resumed"
    assert snapshot.matches("macmini:pid65276", acquired)
    # The finalize kept the exact generation: the same-generation resume stays an
    # idempotent no-op, a delayed foreign resume stays refused.
    assert pause_owner.mark_resumed("macmini:pid65276", acquired)
    assert not pause_owner.mark_resumed("other:pid1", acquired)


def test_rollout_child_start_does_not_finalize_the_pause_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rollout child (gateway local leg) leaves posture `converging` and the
    admission held — the orchestrator's own finally owns that resume boundary, so
    the start must not record the pause as completed while the host is still
    mid-transition."""
    from datetime import UTC, datetime

    from cli.commands import _data_plane_admin_secrets as secrets_mod
    from cli.commands import start as start_mod
    from shared import pause_owner
    from shared.cluster_lock import DeployLease

    owner_path = tmp_path / "deploy-pause-owner.json"
    lock_path = tmp_path / "deploy-pause-owner.lock"
    monkeypatch.setattr(pause_owner, "state_path", lambda: owner_path)
    monkeypatch.setattr(pause_owner, "lock_path", lambda: lock_path)
    monkeypatch.setattr(
        "shared.host_deploy_state.set_posture",
        lambda _p: None,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease",
        lambda: DeployLease(
            holder="rollout:42",
            held_for_s=10,
            expires_in_s=900,
            kind="rollout",
            acquired_at=datetime(2026, 8, 26, 14, 14, 42, tzinfo=UTC),
        ),
    )
    monkeypatch.setattr(
        secrets_mod,
        "ensure_data_plane_admin_secrets",
        lambda **_kw: None,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(start_mod, "cmd_status", lambda: 0)
    monkeypatch.setattr(
        _cli.subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )

    pause_owner.mark_paused("rollout:42", datetime(2026, 8, 26, 14, 14, 42, tzinfo=UTC))

    assert _cli.cmd_start(persist_services=False) == 0

    snapshot = pause_owner.read()
    assert snapshot.status == "paused"
    assert snapshot.matches("rollout:42", datetime(2026, 8, 26, 14, 14, 42, tzinfo=UTC))


def test_rollout_child_keeps_converging_before_parent_readiness(
    monkeypatch: pytest.MonkeyPatch,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """An old parent has no handoff marker, so its executing lease is the
    compatibility proof: the fresh internal start must not revive agents."""
    from cli.commands import _data_plane_admin_secrets as secrets_mod
    from cli.commands import start as start_mod
    from shared.cluster_lock import DeployLease
    from shared.rollout_handoff import ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV

    service, _shell = _fake_session_backends
    postures: list[str] = []
    legacy_upgrade: list[bool] = []

    def _record_legacy_upgrade(*, allow_legacy_upgrade: bool) -> bool:
        legacy_upgrade.append(allow_legacy_upgrade)
        return False

    monkeypatch.delenv(ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV, raising=False)
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease",
        lambda: DeployLease(
            holder="old-parent:42",
            held_for_s=10,
            expires_in_s=900,
            kind="rollout",
        ),
    )
    monkeypatch.setattr("shared.host_deploy_state.set_posture", postures.append)
    monkeypatch.setattr(
        secrets_mod,
        "ensure_data_plane_admin_secrets",
        _record_legacy_upgrade,
    )
    monkeypatch.setattr(start_mod, "cmd_status", lambda: 0)
    monkeypatch.setattr(
        _cli.subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _cli.cmd_start(persist_services=False)

    assert rc == 0
    assert postures[-1] == "converging"
    assert _sess("restarter") not in service.created
    assert legacy_upgrade == [False]


def test_handoff_capable_rollout_child_may_commit_credential_transition(
    monkeypatch: pytest.MonkeyPatch,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """The follow-up rollout carries v1 proof: credential mutation becomes
    legal while admission remains behind the same resume boundary."""
    from cli.commands import _data_plane_admin_secrets as secrets_mod
    from cli.commands import start as start_mod
    from shared.rollout_handoff import (
        ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV,
        ROLLOUT_PARENT_CREDENTIAL_HANDOFF_VERSION,
    )

    service, _shell = _fake_session_backends
    legacy_upgrade: list[bool] = []

    def _record_legacy_upgrade(*, allow_legacy_upgrade: bool) -> bool:
        legacy_upgrade.append(allow_legacy_upgrade)
        return False

    monkeypatch.setenv(
        ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV,
        ROLLOUT_PARENT_CREDENTIAL_HANDOFF_VERSION,
    )
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease",
        lambda: pytest.fail("the versioned parent marker is authoritative"),
    )
    monkeypatch.setattr(
        secrets_mod,
        "ensure_data_plane_admin_secrets",
        _record_legacy_upgrade,
    )
    monkeypatch.setattr(start_mod, "cmd_status", lambda: 0)
    monkeypatch.setattr(
        _cli.subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _cli.cmd_start(persist_services=False) == 0
    assert legacy_upgrade == [True]
    assert _sess("restarter") not in service.created
    assert ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV not in os.environ


def test_phase_b_pure_runner_restores_idle_posture_and_agent_host(
    monkeypatch: pytest.MonkeyPatch,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """Phase B uses the same internal ``ava start --persist-services`` shape
    under the executing cluster lease, but a pure runner must finish its local
    transition instead of inheriting the gateway parent's resume boundary."""
    from cli.commands import start as start_mod
    from shared.cluster_lock import DeployLease

    service, _shell = _fake_session_backends
    postures: list[str] = []
    monkeypatch.setattr(
        "shared.machine.machine_role",
        lambda: frozenset({"agent-runner"}),
    )
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease",
        lambda: DeployLease(
            holder="gateway-rollout:42",
            held_for_s=10,
            expires_in_s=900,
            kind="rollout",
        ),
    )
    monkeypatch.setattr("shared.host_deploy_state.set_posture", postures.append)
    monkeypatch.setattr(start_mod, "cmd_status", lambda: 0)
    monkeypatch.setattr(
        _cli.subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _cli.cmd_start(persist_services=False) == 0
    assert postures[-1] == "idle"
    assert _sess("agent-host") in service.created


def test_operator_start_refuses_executing_rollout_before_migrations(
    monkeypatch: pytest.MonkeyPatch,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """A concurrent operator cannot become a second schema writer."""
    from cli.commands import start as start_mod
    from shared.cluster_lock import DeployLease

    service, _shell = _fake_session_backends
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease",
        lambda: DeployLease(
            holder="rollout:42",
            held_for_s=10,
            expires_in_s=900,
            kind="rollout",
        ),
    )
    monkeypatch.setattr(
        start_mod,
        "cmd_migrations_apply",
        lambda: pytest.fail("migration ran before rollout refusal"),
    )

    assert _cli.cmd_start() == 1
    assert service.created == []


def test_rollout_lease_read_failure_is_before_migrations(
    monkeypatch: pytest.MonkeyPatch,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """An unreadable rollout authority fails closed before schema mutation."""
    from cli.commands import start as start_mod

    service, _shell = _fake_session_backends

    def _unreadable() -> None:
        raise RuntimeError("lease unavailable")

    monkeypatch.setattr("shared.cluster_lock.read_update_lease", _unreadable)
    monkeypatch.setattr(
        start_mod,
        "cmd_migrations_apply",
        lambda: pytest.fail("migration ran with unreadable rollout authority"),
    )

    assert _cli.cmd_start() == 1
    assert service.created == []


def test_pending_credential_transition_replays_before_migrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash journal is adopted before the first schema client is opened."""
    from cli.commands import _data_plane_admin_secrets as secrets_mod
    from cli.commands import start as start_mod

    order: list[str] = []
    monkeypatch.setattr(
        secrets_mod,
        "resume_pending_data_plane_admin_secrets",
        lambda: order.append("resume"),
    )
    monkeypatch.setattr(
        start_mod,
        "cmd_migrations_apply",
        lambda: order.append("migrate") or 0,
    )
    monkeypatch.setattr(start_mod, "cmd_status", lambda: 0)
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: None)
    monkeypatch.setattr(
        _cli.subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _cli.cmd_start() == 0
    assert order[:2] == ["resume", "migrate"]


def test_machine_description_setup_field_writes_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cli.commands import _setup

    monkeypatch.setattr(settings.general, "machine_description", "")
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    field = next(f for f in _setup._SETUP_FIELDS if f.name == "machine_description")
    # arg provided → write file + return value
    assert _setup._resolve_setup_field(field, "voice IO + browser") == "voice IO + browser"
    assert (tmp_path / "machine_description").read_text() == "voice IO + browser"
    # no env, no file, no arg → optional field returns None
    no_file_tmp = tmp_path / "subdir_no_file"
    no_file_tmp.mkdir()
    monkeypatch.setattr("shared.paths.ava_home", lambda: no_file_tmp)
    assert _setup._resolve_setup_field(field, None) is None


def test_fan_out_classifies_dispatch_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    """_dispatch_one_and_wait maps direct-dial outcomes to the (ok / fatal /
    unreachable) triplet that upstream print/abort logic still depends on."""
    from ops import cluster_rpc as cr

    async def _ok(*_a, **_kw):
        return {}

    async def _unreachable(*_a, **_kw):
        raise cr.ClusterOpUnreachable("simulated")

    async def _fail(*_a, **_kw):
        raise cr.ClusterOpFailed({"error": "agent-runner blew up"})

    # ok
    monkeypatch.setattr(cr, "dispatch_to_machine", _ok)  # pyright: ignore[reportUnknownArgumentType]
    name, status, _ = asyncio.run(_cli._dispatch_one_and_wait("wsl", "cluster_stop", 5.0))
    assert (name, status) == ("wsl", "ok")

    # unreachable ops server
    monkeypatch.setattr(cr, "dispatch_to_machine", _unreachable)  # pyright: ignore[reportUnknownArgumentType]
    name, status, detail = asyncio.run(_cli._dispatch_one_and_wait("wsl", "cluster_stop", 5.0))
    assert (name, status) == ("wsl", "unreachable")
    assert "unreachable" in detail

    # op ran but failed -> fatal
    monkeypatch.setattr(cr, "dispatch_to_machine", _fail)  # pyright: ignore[reportUnknownArgumentType]
    name, status, detail = asyncio.run(_cli._dispatch_one_and_wait("wsl", "cluster_stop", 5.0))
    assert (name, status) == ("wsl", "fatal")
    assert "agent-runner blew up" in detail


def test_cmd_update_gateway_default_posts_rollout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gateway-capable host's default is the SAME POST every host sends —
    no local spawn branch (user ruling 2026-08-21, issue #216). The gateway
    answers by starting the detached rollout; the CLI just prints the
    session/log it is told about."""
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"gateway"}))

    def _no_local(*_a, **_kw):
        raise AssertionError(
            "foreground `ava cluster update` must not run the in-process orchestration"
        )

    monkeypatch.setattr(_cli, "_run_gateway_orchestration", _no_local)  # pyright: ignore[reportUnknownArgumentType]
    from typing import cast

    calls: list[tuple[str, dict[str, object]]] = []

    class _Resp:
        status_code = 202

        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, object]:
            return {"session": "ava-rollout", "log": "/var/log/u.log"}

    def _fake_post(url: str, **_kw: object) -> _Resp:
        calls.append((url, cast(dict[str, object], _kw.get("json"))))
        return _Resp()

    monkeypatch.setattr("httpx.post", _fake_post)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_update()
    assert rc == 0
    # a human-invoked `ava cluster update` self-identifies as cli:<machine>
    assert calls[0][0] == "http://gw:8000/api/cluster/rollout"
    assert cast(str, calls[0][1]["origin"]).startswith("cli:")
    assert "ava-rollout" in capsys.readouterr().out


def test_cmd_update_local_forces_in_process_orchestration(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ava cluster update --local` (what the detached rollout session runs) forces the
    in-process gateway orchestration and never POSTs."""
    from cli.commands import update as _up_mod

    monkeypatch.setattr(_up_mod, "_repo_root", lambda: Path("/repo"))
    monkeypatch.setattr(_up_mod, "ava_home", lambda: Path("/home"))
    monkeypatch.setattr(_up_mod, "get_record", lambda _home: None)  # pyright: ignore[reportUnknownArgumentType]

    def _no_post(*_a: object, **_kw: object) -> None:
        raise AssertionError("--local must not POST the gateway")

    monkeypatch.setattr("httpx.post", _no_post)  # pyright: ignore[reportUnknownArgumentType]
    ran: list[bool] = []
    monkeypatch.setattr(
        _cli,
        "_run_gateway_orchestration",
        lambda *_a, **_kw: ran.append(True) or 0,  # pyright: ignore[reportUnknownArgumentType]
    )
    rc = _cli.cmd_update(local=True)
    assert rc == 0
    assert ran == [True]


def test_cmd_update_rollout_conflict_and_noop_are_friendly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rollout endpoint's ordinary refusals — 409 update-in-flight and 422
    nothing-to-update — print one stderr line and exit 1 / 0 respectively,
    not a raw traceback (the deploy-window case a second operator most often
    hits)."""
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")

    class _Resp:
        def __init__(self, status_code: int, detail: str):
            self.status_code = status_code
            self._detail = detail

        def raise_for_status(self) -> None:
            import httpx

            if self.status_code >= 400:
                request = httpx.Request("POST", "http://gw:8000")
                response = httpx.Response(self.status_code, request=request)
                raise httpx.HTTPStatusError(self._detail, request=request, response=response)

        def json(self) -> dict[str, object]:
            return {"detail": self._detail}

    monkeypatch.setattr(
        "httpx.post",
        lambda *_a, **_kw: _Resp(409, "deploy already in flight"),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert _cli.cmd_update() == 1
    assert "deploy already in flight" in capsys.readouterr().err

    monkeypatch.setattr(
        "httpx.post",
        lambda *_a, **_kw: _Resp(422, "already up to date"),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert _cli.cmd_update() == 0  # a no-op update is not a failure
    assert "already up to date" in capsys.readouterr().err
