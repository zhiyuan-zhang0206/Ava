"""validate-before-kill on the agent-runner self-update leg.

The pre-checkout updater force-checks out the target and syncs the venv, then a
fresh interpreter vets the checked-out tree's migrations/layout and the `ava`
launcher it will relaunch through, all BEFORE the graceful stop. Every one of
those failures would otherwise only surface after the stop has taken the host
down, and step 5 is the only thing that brings it back: a broken migrations/
layout fails the trailing `ava start` migrate, and a launcher at the wrong path
raises FileNotFoundError out of a host that is already dark. The fresh leg
instead aborts (reverting the checkout where it made one) with the host still
serving.

The launcher path is platform-named — `.venv/bin/ava` vs `.venv\\Scripts\\ava.exe`
— and used to be hardcoded POSIX, so a Windows agent-runner stopped itself and
then could not start: the `TestLauncherGate` cases below swap the platform
backend rather than the host, so they run anywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import cli.commands as _ns
from cli.commands import _update_agent_runner as ar
from shared import host_deploy_state
from shared.exit_codes import RESTART_DECLINED_EXIT_CODE
from shared.migrations import MigrationLayoutError
from shared.platform_backend import MacPlatformBackend, WindowsPlatformBackend


class _FakeCompleted:
    returncode = 0


def _sync_verified(_repo: Path, *, timeout_s: float = 600.0) -> _FakeCompleted:
    del timeout_s
    return _FakeCompleted()


def _raise_layout(_ref, **_kw):
    raise MigrationLayoutError("duplicate migration name: '20260719T143000_add-foo'")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout whose venv carries the POSIX `ava` launcher (what `uv sync`
    leaves behind), so the leg gets past its launcher gate."""
    launcher = tmp_path / ".venv" / "bin" / "ava"
    launcher.parent.mkdir(parents=True)
    launcher.touch()
    return tmp_path


def test_agent_runner_reverts_and_skips_stop_on_broken_layout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checkouts: list[str] = []
    stopped: list[bool] = []

    def _checkout(sha: str) -> str:
        checkouts.append(sha)
        return "oldsha0000"  # from_sha returned by the forward checkout

    monkeypatch.setattr(host_deploy_state, "try_acquire_updater_lock", lambda: False)
    monkeypatch.setattr(ar, "git_checkout_sha", _checkout)
    monkeypatch.setattr(ar.subprocess, "run", lambda *_a, **_k: _FakeCompleted())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(ar, "run_uv_sync_verified", _sync_verified)
    monkeypatch.setattr(ar, "validate_migrations_at_ref", _raise_layout)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ns, "_do_stop", lambda *_a, **_k: stopped.append(True))  # pyright: ignore[reportUnknownArgumentType]

    rc = ar._run_agent_runner_self_update(
        Path("/unused"),
        target_sha="newsha1111",
        from_sha="oldsha0000",
        post_checkout=True,
    )

    assert rc == 1
    assert checkouts == ["oldsha0000"], "must revert to the prior commit"
    assert stopped == [], "must not stop after a failed layout vet"
    assert "reverting" in capsys.readouterr().err


def test_agent_runner_proceeds_to_stop_on_valid_layout(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    checkouts: list[str] = []
    stopped: list[bool] = []

    monkeypatch.setattr(host_deploy_state, "try_acquire_updater_lock", lambda: False)
    monkeypatch.setattr(ar, "git_checkout_sha", lambda sha: checkouts.append(sha) or "oldsha0000")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(ar.subprocess, "run", lambda *_a, **_k: _FakeCompleted())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(ar, "run_uv_sync_verified", _sync_verified)
    monkeypatch.setattr(ar, "validate_migrations_at_ref", lambda _ref, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ns, "_do_stop", lambda *_a, **_k: stopped.append(True))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ns, "_preflight_probes", lambda: 0)  # skip real gateway probe
    monkeypatch.setattr(_ns, "_quiesce_local_agents", lambda _mode: True)  # pyright: ignore[reportUnknownArgumentType]

    rc = ar._run_agent_runner_self_update(
        repo,
        target_sha="newsha1111",
        from_sha="oldsha0000",
        post_checkout=True,
    )

    assert rc == 0
    assert checkouts == [], "the post-checkout leg must not check out again"
    assert stopped == [True]


def test_agent_runner_aborts_when_preflight_probes_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When preflight probes fail, abort without stopping services."""
    stopped: list[bool] = []

    monkeypatch.setattr(host_deploy_state, "try_acquire_updater_lock", lambda: False)
    monkeypatch.setattr(ar, "git_checkout_sha", lambda _sha: "oldsha0000")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(ar.subprocess, "run", lambda *_a, **_k: _FakeCompleted())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(ar, "run_uv_sync_verified", _sync_verified)
    monkeypatch.setattr(ar, "validate_migrations_at_ref", lambda _ref, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ns, "_do_stop", lambda *_a, **_k: stopped.append(True))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ns, "_preflight_probes", lambda: 1)  # simulate failure
    monkeypatch.setattr(_ns, "_release_self_heal_pause", lambda: None)

    rc = ar._run_agent_runner_self_update(
        Path("/unused"),
        target_sha="newsha1111",
        from_sha="oldsha0000",
        post_checkout=True,
    )

    # Not merely non-zero: a refusal BEFORE the stop reports its own code, so a
    # caller can tell "still serving" from "stopped and possibly down".
    assert rc == RESTART_DECLINED_EXIT_CODE, "preflight failure must propagate non-zero"
    assert stopped == [], "must not stop services when preflight fails"


def test_agent_runner_restart_only_also_runs_preflight(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    """restart_only path also runs preflight before stopping."""
    stopped: list[bool] = []
    preflight_called: list[bool] = []

    def fake_preflight() -> int:
        preflight_called.append(True)
        return 0

    monkeypatch.setattr(_ns, "_do_stop", lambda *_a, **_k: stopped.append(True))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ns, "_preflight_probes", fake_preflight)
    monkeypatch.setattr(_ns, "_quiesce_local_agents", lambda _mode: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(ar.subprocess, "run", lambda *_a, **_k: _FakeCompleted())  # pyright: ignore[reportUnknownArgumentType]

    rc = ar._run_agent_runner_self_update(repo, restart_only=True)

    assert rc == 0
    assert preflight_called == [True], "restart_only must still run preflight"
    assert stopped == [True], "services must be stopped after successful preflight"


class TestLauncherGate:
    """Step 5 relaunches the host through `<repo>/.venv/<bindir>/ava`. That path is
    platform-named, and reaching it with the wrong name after the stop leaves the
    host dark with nothing able to restart it."""

    def test_aborts_before_stopping_when_the_launcher_is_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stopped: list[bool] = []
        monkeypatch.setattr(_ns, "_do_stop", lambda *_a, **_k: stopped.append(True))  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(_ns, "_preflight_probes", lambda: 0)
        monkeypatch.setattr(ar.subprocess, "run", lambda *_a, **_k: _FakeCompleted())  # pyright: ignore[reportUnknownArgumentType]

        rc = ar._run_agent_runner_self_update(tmp_path, restart_only=True)

        assert rc == 1
        assert stopped == [], "a missing launcher must abort while the host still serves"

    def test_posix_launcher_is_the_one_that_gets_run(
        self, monkeypatch: pytest.MonkeyPatch, repo: Path
    ) -> None:
        monkeypatch.setattr(ar, "platform_backend", MacPlatformBackend)
        launched: list[list[str]] = []

        def _run(argv, *_a, **_k):  # type: ignore[no-untyped-def]
            launched.append(argv)  # pyright: ignore[reportUnknownArgumentType]
            return _FakeCompleted()

        monkeypatch.setattr(_ns, "_do_stop", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(_ns, "_preflight_probes", lambda: 0)
        monkeypatch.setattr(_ns, "_quiesce_local_agents", lambda _mode: True)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(ar.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]

        assert ar._run_agent_runner_self_update(repo, restart_only=True) == 0
        assert launched[-1] == [
            str(repo / ".venv" / "bin" / "ava"),
            "start",
            "--persist-services",
            "--updater-telemetry",
        ]

    def test_windows_resolves_scripts_ava_exe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The bug: on Windows the venv bin dir is `Scripts` and the launcher
        carries a `.exe` suffix, so the hardcoded `.venv/bin/ava` never existed and
        step 5 raised FileNotFoundError on an already-stopped host."""
        monkeypatch.setattr(ar, "platform_backend", WindowsPlatformBackend)
        launcher = tmp_path / ".venv" / "Scripts" / "ava.exe"
        launcher.parent.mkdir(parents=True)
        launcher.touch()
        launched: list[list[str]] = []

        def _run(argv, *_a, **_k):  # type: ignore[no-untyped-def]
            launched.append(argv)  # pyright: ignore[reportUnknownArgumentType]
            return _FakeCompleted()

        monkeypatch.setattr(_ns, "_do_stop", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(_ns, "_preflight_probes", lambda: 0)
        monkeypatch.setattr(_ns, "_quiesce_local_agents", lambda _mode: True)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(ar.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]

        assert ar._run_agent_runner_self_update(tmp_path, restart_only=True) == 0
        assert launched[-1][0] == str(launcher)

    def test_windows_aborts_when_only_the_posix_launcher_exists(
        self, monkeypatch: pytest.MonkeyPatch, repo: Path
    ) -> None:
        """`.venv/bin/ava` present but `.venv/Scripts/ava.exe` absent is exactly the
        `win` box's layout — abort, do not stop."""
        monkeypatch.setattr(ar, "platform_backend", WindowsPlatformBackend)
        stopped: list[bool] = []
        monkeypatch.setattr(_ns, "_do_stop", lambda *_a, **_k: stopped.append(True))  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(_ns, "_preflight_probes", lambda: 0)
        monkeypatch.setattr(ar.subprocess, "run", lambda *_a, **_k: _FakeCompleted())  # pyright: ignore[reportUnknownArgumentType]

        assert ar._run_agent_runner_self_update(repo, restart_only=True) == 1
        assert stopped == []

    def test_launch_failure_after_the_stop_is_reported_not_traced(
        self, monkeypatch: pytest.MonkeyPatch, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Vetted-then-vanished: services are already down, so the operator gets the
        path and the recovery instruction instead of a bare traceback."""
        monkeypatch.setattr(_ns, "_do_stop", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(_ns, "_preflight_probes", lambda: 0)
        monkeypatch.setattr(_ns, "_quiesce_local_agents", lambda _mode: True)  # pyright: ignore[reportUnknownArgumentType]

        def _run(argv, *_a, **_k):  # type: ignore[no-untyped-def]
            if argv[0].endswith("ava"):  # pyright: ignore[reportUnknownMemberType]
                raise FileNotFoundError(argv[0])  # pyright: ignore[reportUnknownArgumentType]
            return _FakeCompleted()

        monkeypatch.setattr(ar.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]

        assert ar._run_agent_runner_self_update(repo, restart_only=True) == 1
        assert "cannot restart itself" in capsys.readouterr().err


class TestModuleEntry:
    """`python -m cli.commands._update_agent_runner` — the R1-6 detached-session
    entry (execution-shape convergence). `spawn_update`'s POSIX chain runs this
    module, so the flags must map onto exactly the kwargs the ops-server payload
    carries, and the entry's rc must pass straight through to the wrapper's
    `[session-exit] rc=` line."""

    def test_flags_map_onto_the_self_update_kwargs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict] = []
        monkeypatch.setattr(
            ar,
            "_run_agent_runner_self_update",
            lambda _repo, **kw: calls.append(kw) or 0,  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        )
        assert ar.main(["--target-sha", "abc1234", "--mode", "none", "--force-reap"]) == 0
        assert calls == [
            {
                "target_sha": "abc1234",
                "restart_only": False,
                "mode": "none",
                "force_reap": True,
                "handoff_generation": None,
                "post_checkout": False,
                "from_sha": None,
            }
        ]

    def test_restart_only_and_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict] = []
        monkeypatch.setattr(
            ar,
            "_run_agent_runner_self_update",
            lambda _repo, **kw: calls.append(kw) or 7,  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        )
        assert ar.main(["--restart-only"]) == 7
        assert calls == [
            {
                "target_sha": None,
                "restart_only": True,
                "mode": "smooth",
                "force_reap": False,
                "handoff_generation": None,
                "post_checkout": False,
                "from_sha": None,
            }
        ]

    def test_rejects_unknown_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(SystemExit):
            ar.main(["--mode", "bogus"])
