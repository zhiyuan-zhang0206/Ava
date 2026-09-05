"""A post-checkout updater must never import new-tree code in its old process.

The 2026-09-01 runner failure checked out faf061d from f22f5eb, then imported
``ops.updater_outcome`` in the still-resident updater interpreter. The new module
needed ``STAGE_NO_PROGRESS_TIMEOUT_S`` from ``shared.deploy_timing``; that module
was still the old one in ``sys.modules``, so the import failed. These probes use
the real current modules around a deliberately old ``deploy_timing`` copy to pin
the mechanism and show that an exec boundary resolves it.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli.commands as _cli
from cli.commands import _update_agent_runner as _runner
from shared.deploy_timing import UV_SYNC_TIMEOUT_S

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STAGE_TIMEOUT_LINE = "STAGE_NO_PROGRESS_TIMEOUT_S = 675.0"


def _make_skew_tree(tmp_path: Path) -> Path:
    """Build an old shared module beside the current outcome reader."""
    root = tmp_path / "skew-tree"
    shared = root / "shared"
    ops = root / "ops"
    shared.mkdir(parents=True)
    ops.mkdir()
    (shared / "__init__.py").touch()
    (ops / "__init__.py").touch()

    deploy_timing = (_REPO_ROOT / "shared" / "deploy_timing.py").read_text()
    assert _STAGE_TIMEOUT_LINE in deploy_timing
    (shared / "deploy_timing.py").write_text(
        "\n".join(line for line in deploy_timing.splitlines() if line != _STAGE_TIMEOUT_LINE) + "\n"
    )
    (ops / "updater_outcome.py").write_text((_REPO_ROOT / "ops" / "updater_outcome.py").read_text())
    return root


def _old_module_driver(tree: Path) -> str:
    """Load only the deliberately old deploy module, then expose real siblings."""
    return f"""
import sys

tree = {str(tree)!r}
repo = {str(_REPO_ROOT)!r}
sys.path.insert(0, tree)
import shared.deploy_timing
shared.__path__.append(repo + "/shared")
"""


def test_skew_in_process_import_fails_with_missing_symbol(tmp_path: Path) -> None:
    """A fresh outcome module fails against the updater's old cached dependency."""
    tree = _make_skew_tree(tmp_path)
    proc = subprocess.run(  # noqa: S603 — fixed argv and test-controlled source tree
        [
            sys.executable,
            "-c",
            _old_module_driver(tree) + "\nimport ops.updater_outcome\n",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode != 0
    assert "ImportError" in proc.stderr
    assert "STAGE_NO_PROGRESS_TIMEOUT_S" in proc.stderr


def test_fresh_interpreter_after_checkout_imports_cleanly(tmp_path: Path) -> None:
    """Replacing the old image loads the current compatible module set."""
    tree = _make_skew_tree(tmp_path)
    proc = subprocess.run(  # noqa: S603 — fixed argv and test-controlled source tree
        [
            sys.executable,
            "-c",
            _old_module_driver(tree)
            + "\nimport os\n"
            + "os.execv(sys.executable, [sys.executable, '-c', "
            + repr("import ops.updater_outcome; print('fresh-import-ok')")
            + "])\n",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "fresh-import-ok\n"


def _patch_wrapper_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep wrapper tests hermetic while preserving their control-flow shape."""
    from shared import host_deploy_state, ui_update_state, updater_handoff

    monkeypatch.setattr(host_deploy_state, "try_acquire_updater_lock", lambda: True)
    monkeypatch.setattr(host_deploy_state, "release_updater_lock", lambda: None)
    monkeypatch.setattr(host_deploy_state, "touch_updater_lease", lambda: None)
    monkeypatch.setattr(host_deploy_state, "clear_updater_lease", lambda: None)
    monkeypatch.setattr(ui_update_state, "lifecycle_lock", contextlib.nullcontext)
    monkeypatch.setattr(
        updater_handoff,
        "begin",
        lambda **_kwargs: SimpleNamespace(generation="handoff-generation"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(updater_handoff, "claim_running", lambda *_args, **_kwargs: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(updater_handoff, "clear", lambda *_args: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.cluster.session_name", lambda _name: "ava-updater")  # pyright: ignore[reportUnknownArgumentType]


def test_updater_hands_off_to_fresh_interpreter_after_sync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Checkout/sync must exec before any preflight, quiesce, or stop import."""
    _patch_wrapper_lifecycle(monkeypatch)
    handoff_argv: list[list[str]] = []
    stopped: list[bool] = []

    def sync_verified(
        _repo: Path,
        *,
        timeout_s: float = UV_SYNC_TIMEOUT_S,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(["uv", "sync"], returncode=0)

    monkeypatch.setattr(_runner, "git_checkout_sha", lambda _sha: "oldsha0000")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_runner, "run_uv_sync_verified", sync_verified)
    monkeypatch.setattr("shared.source_integrity.set_installed", lambda _sha: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _runner,
        "_exec_post_checkout",
        lambda argv: handoff_argv.append(argv) or (_ for _ in ()).throw(SystemExit(0)),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_cli, "_do_stop", lambda *_args, **_kwargs: stopped.append(True))  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(SystemExit, match="0"):
        _runner._run_agent_runner_self_update(
            tmp_path,
            target_sha="newsha1111",
            mode="none",
            force_reap=True,
        )

    assert handoff_argv == [
        [
            sys.executable,
            "-m",
            "cli.commands._update_agent_runner",
            "--post-checkout",
            "--target-sha",
            "newsha1111",
            "--from-sha",
            "oldsha0000",
            "--mode",
            "none",
            "--force-reap",
            "--handoff-generation",
            "handoff-generation",
        ]
    ]
    assert stopped == []


def test_handoff_claim_failure_releases_the_updater_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A declined handoff must not strand the mutex until this process exits."""
    from shared import host_deploy_state, ui_update_state, updater_handoff
    from shared.exit_codes import RESTART_DECLINED_EXIT_CODE

    released: list[bool] = []
    monkeypatch.setattr(host_deploy_state, "try_acquire_updater_lock", lambda: True)
    monkeypatch.setattr(host_deploy_state, "release_updater_lock", lambda: released.append(True))
    monkeypatch.setattr(ui_update_state, "lifecycle_lock", contextlib.nullcontext)
    monkeypatch.setattr(
        updater_handoff,
        "begin",
        lambda **_kwargs: SimpleNamespace(generation="handoff-generation"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(updater_handoff, "claim_running", lambda *_args, **_kwargs: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(updater_handoff, "clear", lambda *_args: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.cluster.session_name", lambda _name: "ava-updater")  # pyright: ignore[reportUnknownArgumentType]

    assert _runner._run_agent_runner_self_update(tmp_path, target_sha="newsha1111") == (
        RESTART_DECLINED_EXIT_CODE
    )
    assert released == [True]


def test_post_checkout_leg_runs_validate_to_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fresh image owns every new-tree step through the fresh start child."""
    from shared import host_deploy_state, updater_handoff

    steps: list[str] = []
    launcher = tmp_path / "ava"
    launcher.touch()

    class _Backend:
        def venv_launcher(self, _name: str, root: Path) -> Path:
            assert root == tmp_path
            steps.append("launcher")
            return launcher

    monkeypatch.setattr(host_deploy_state, "touch_updater_lease", lambda: None)
    monkeypatch.setattr(host_deploy_state, "clear_updater_lease", lambda: None)
    monkeypatch.setattr(host_deploy_state, "release_updater_lock", lambda: None)
    monkeypatch.setattr(
        host_deploy_state,
        "try_acquire_updater_lock",
        lambda: False,  # POSIX guard: the inherited flock still held -> continuation proceeds
    )
    monkeypatch.setattr(
        updater_handoff,
        "begin",
        lambda **_kwargs: (_ for _ in ()).throw(  # pyright: ignore[reportUnknownArgumentType]
            AssertionError("post-checkout must not begin a handoff")
        ),
    )
    monkeypatch.setattr(
        updater_handoff,
        "claim_running",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(  # pyright: ignore[reportUnknownArgumentType]
            AssertionError("post-checkout must not claim a handoff")
        ),
    )
    monkeypatch.setattr(updater_handoff, "clear", lambda *_args: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _runner,
        "validate_migrations_at_ref",
        lambda _sha, **_kwargs: steps.append("validate"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_runner, "platform_backend", _Backend)
    monkeypatch.setattr(_runner, "_refresh_builtin_skills", lambda *_args: steps.append("skills"))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_preflight_probes", lambda: steps.append("preflight") or 0)
    monkeypatch.setattr(
        _cli,
        "_quiesce_local_agents",
        lambda _mode: steps.append("quiesce") or True,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_cli, "_do_stop", lambda *_args, **_kwargs: steps.append("stop"))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _runner.subprocess,
        "run",
        lambda *_args, **_kwargs: steps.append("start") or SimpleNamespace(returncode=0),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert (
        _runner._run_agent_runner_self_update(
            tmp_path,
            target_sha="newsha1111",
            from_sha="oldsha0000",
            post_checkout=True,
            mode="smooth",
        )
        == 0
    )
    assert steps == ["validate", "preflight", "launcher", "skills", "quiesce", "stop", "start"]


def test_post_checkout_fails_fast_when_the_flock_did_not_survive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A POSIX post-checkout image that ACQUIRES the lock has lost the exec'd
    flock — the stop/start leg must not run unlocked (task #1181's race)."""
    from shared import host_deploy_state, updater_handoff

    monkeypatch.setattr(host_deploy_state, "touch_updater_lease", lambda: None)
    monkeypatch.setattr(host_deploy_state, "clear_updater_lease", lambda: None)
    monkeypatch.setattr(host_deploy_state, "release_updater_lock", lambda: None)
    monkeypatch.setattr(host_deploy_state, "try_acquire_updater_lock", lambda: True)
    monkeypatch.setattr(updater_handoff, "clear", lambda *_args: None)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(RuntimeError, match="flock did not survive"):
        _runner._run_agent_runner_self_update(
            tmp_path,
            target_sha="newsha1111",
            from_sha="oldsha0000",
            post_checkout=True,
            mode="none",
        )


def test_post_checkout_flag_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The internal continuation cannot run without the prior checkout revision."""
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        _runner,
        "_run_agent_runner_self_update",
        lambda _repo, **kwargs: calls.append(kwargs) or 0,  # pyright: ignore[reportUnknownArgumentType]
    )

    assert (
        _runner.main(
            [
                "--post-checkout",
                "--target-sha",
                "newsha1111",
                "--from-sha",
                "oldsha0000",
                "--mode",
                "none",
            ]
        )
        == 0
    )
    assert calls == [
        {
            "target_sha": "newsha1111",
            "restart_only": False,
            "mode": "none",
            "force_reap": False,
            "handoff_generation": None,
            "post_checkout": True,
            "from_sha": "oldsha0000",
        }
    ]
    for bad in (
        ["--post-checkout"],  # neither sha
        ["--post-checkout", "--from-sha", "oldsha0000"],  # missing target
        ["--post-checkout", "--target-sha", "newsha1111"],  # missing from
        ["--post-checkout", "--restart-only"],  # contradictory mode
    ):
        with pytest.raises(SystemExit):
            _runner.main(bad)
