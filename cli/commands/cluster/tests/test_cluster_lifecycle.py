"""`ava cluster destroy`: a confirmed decommission of the host's cluster, which
closes native ownership before marking the home detached."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from cli.commands.cluster import home as lifecycle


def _detached(home: Path) -> bool:
    marker = home / "destroy-intent.json"
    return marker.exists() and '"detached"' in marker.read_text()


class _Stream(io.StringIO):
    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture
def roles() -> list[str]:
    return ["agent-runner"]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, roles: list[str]) -> Path:
    """A born home: `AVA_HOME` names it and its start intent records the cluster."""
    home = (tmp_path / "home").resolve()
    home.mkdir(mode=0o700)
    monkeypatch.setenv("AVA_HOME", str(home))

    def read_intent(_home: Path) -> dict[str, object]:
        return {"roles": roles}

    def stop() -> int:
        return 0

    def unregister_jobs() -> None:
        return None

    def helper(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("cli.start_identity.read_intent", read_intent)
    monkeypatch.setattr(lifecycle, "_stop_cluster", stop)
    monkeypatch.setattr(lifecycle, "_unregister_scheduled_jobs", unregister_jobs)
    monkeypatch.setattr("services.desktop.permissions_helper.launchd_job.unregister_helper", helper)
    return home


def _gateway_intent(_home: Path) -> dict[str, object]:
    return {"roles": ["gateway"]}


def _no_intent(_home: Path) -> None:
    return None


def _nothing_to_stop() -> int:
    return 0


def _no_jobs() -> None:
    return None


def _no_helper(*_args: object, **_kwargs: object) -> None:
    return None


def _at_a_terminal(monkeypatch: pytest.MonkeyPatch, *answers: str | type[BaseException]) -> Mock:
    """A person at a terminal who types `answers` in turn (an exception is raised)."""
    monkeypatch.setattr(lifecycle, "_interactive", lambda: True)
    prompt = Mock(side_effect=list(answers))
    monkeypatch.setattr("builtins.input", prompt)
    return prompt


def test_destroy_checks_helper_before_detaching(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from base.config import settings
    from services.desktop.permissions_helper import launchd_job

    seen: list[Path] = []

    def unregister(target: Path, *, helper_port: int) -> None:
        assert (home / "destroy-intent.json").exists()
        assert helper_port == settings.services.permissions_helper_port
        seen.append(target)

    monkeypatch.setattr(launchd_job, "unregister_helper", unregister)
    _at_a_terminal(monkeypatch, str(home))
    assert lifecycle.cmd_cluster_destroy() == 0
    assert seen == [home]
    assert _detached(home)


# --- the confirmation ---------------------------------------------------------


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty"), [(False, False), (True, False), (False, True)]
)
def test_destroy_refuses_without_an_interactive_terminal(
    home: Path, monkeypatch: pytest.MonkeyPatch, stdin_tty: bool, stdout_tty: bool
) -> None:
    """Both streams must be terminals: there is no flag that skips the prompt."""
    prompt = Mock(side_effect=AssertionError("prompted without a terminal"))
    monkeypatch.setattr("builtins.input", prompt)
    monkeypatch.setattr(sys, "stdin", _Stream(tty=stdin_tty))
    err = _Stream(tty=False)
    monkeypatch.setattr(sys, "stdout", _Stream(tty=stdout_tty))
    monkeypatch.setattr(sys, "stderr", err)
    stop = Mock()
    monkeypatch.setattr(lifecycle, "_stop_cluster", stop)

    assert lifecycle.cmd_cluster_destroy() == 1
    assert "interactive terminal" in err.getvalue()
    prompt.assert_not_called()
    stop.assert_not_called()
    assert not (home / "destroy-intent.json").exists()


@pytest.mark.parametrize("answer", ["", "y", "yes", "/somewhere/else", "home"])
def test_a_different_answer_changes_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    _at_a_terminal(monkeypatch, answer)
    stop = Mock()
    monkeypatch.setattr(lifecycle, "_stop_cluster", stop)
    data = home / "pg"
    data.mkdir()

    assert lifecycle.cmd_cluster_destroy(drop_db=True) == 1
    stop.assert_not_called()
    assert data.is_dir()
    assert not (home / "destroy-intent.json").exists()


def test_end_of_input_changes_nothing(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _at_a_terminal(monkeypatch, EOFError)
    stop = Mock()
    monkeypatch.setattr(lifecycle, "_stop_cluster", stop)

    assert lifecycle.cmd_cluster_destroy() == 1
    stop.assert_not_called()
    assert not (home / "destroy-intent.json").exists()


@pytest.mark.parametrize("roles", [["agent-runner", "gateway"]])
def test_the_prompt_says_what_happens_and_what_is_deleted(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from base.db.pg_admin import pg_socket_path

    _at_a_terminal(monkeypatch, str(home))
    assert lifecycle.cmd_cluster_destroy(drop_db=True) == 0

    out = capsys.readouterr().out
    assert str(home) in out and "agent-runner, gateway" in out
    assert "serves the gateway" in out
    assert "cannot be undone" in out
    for directory in (home / "pg", home / "redis", pg_socket_path(home)):
        assert str(directory) in out


def test_the_prompt_lists_no_deletion_without_drop_db(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _at_a_terminal(monkeypatch, str(home))
    assert lifecycle.cmd_cluster_destroy() == 0

    out = capsys.readouterr().out
    assert "DELETE" not in out and "cannot be undone" not in out
    assert "serves the gateway" not in out


# --- what destroy does ---------------------------------------------------------


def test_the_default_home_can_be_destroyed(
    default_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It used to be refused outright; the typed path is what guards it now."""
    prod = (default_home / ".ava").resolve()
    prod.mkdir(mode=0o700)
    monkeypatch.setattr("cli.start_identity.read_intent", _gateway_intent)
    monkeypatch.setattr(lifecycle, "_stop_cluster", _nothing_to_stop)
    monkeypatch.setattr(lifecycle, "_unregister_scheduled_jobs", _no_jobs)
    monkeypatch.setattr(
        "services.desktop.permissions_helper.launchd_job.unregister_helper", _no_helper
    )
    _at_a_terminal(monkeypatch, str(prod))

    assert lifecycle.cmd_cluster_destroy() == 0
    assert _detached(prod)


def test_stop_failure_leaves_home_attached_and_never_unloads_helper(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "_stop_cluster", lambda: 4)
    unload = Mock()
    monkeypatch.setattr("services.desktop.permissions_helper.launchd_job.unregister_helper", unload)
    _at_a_terminal(monkeypatch, str(home))

    assert lifecycle.cmd_cluster_destroy() == 4
    assert not _detached(home)
    unload.assert_not_called()


@pytest.mark.parametrize("stage", ["jobs", "helper"])
def test_ambiguous_cleanup_leaves_home_attached(
    home: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("ownership unknown")

    if stage == "jobs":
        monkeypatch.setattr(lifecycle, "_unregister_scheduled_jobs", fail)
    else:
        monkeypatch.setattr(
            "services.desktop.permissions_helper.launchd_job.unregister_helper", fail
        )
    _at_a_terminal(monkeypatch, str(home))

    assert lifecycle.cmd_cluster_destroy() == 1
    assert not _detached(home)


def test_destroy_preserves_credentials_and_data(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = home / ".env"
    env.write_text("PRIVATE_KEY=retain\n")
    data = home / "pg" / "data"
    data.mkdir(parents=True)
    _at_a_terminal(monkeypatch, str(home))

    assert lifecycle.cmd_cluster_destroy() == 0
    assert env.read_text() == "PRIVATE_KEY=retain\n"
    assert data.is_dir()


def test_drop_db_removes_the_data_directories_after_the_stop(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / "pg" / "data").mkdir(parents=True)
    (home / "redis").mkdir()
    (home / ".env").write_text("KEEP=1\n")
    order: list[str] = []

    def stop() -> int:
        assert (home / "pg").is_dir()
        order.append("stop")
        return 0

    monkeypatch.setattr(lifecycle, "_stop_cluster", stop)
    _at_a_terminal(monkeypatch, str(home))

    assert lifecycle.cmd_cluster_destroy(drop_db=True) == 0
    assert order == ["stop"]
    assert not (home / "pg").exists() and not (home / "redis").exists()
    assert (home / ".env").read_text() == "KEEP=1\n"


def test_destroy_refuses_a_home_without_a_start_intent(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cli.start_identity.read_intent", _no_intent)
    prompt = _at_a_terminal(monkeypatch)

    assert lifecycle.cmd_cluster_destroy() == 1
    prompt.assert_not_called()
    assert not (home / "destroy-intent.json").exists()


def test_interrupted_cleanup_retries_to_completion(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cleanup that could not finish leaves the home attached with its destroy
    intent retained; running destroy again completes it."""
    target = "services.desktop.permissions_helper.launchd_job.unregister_helper"

    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("interrupted detach")

    def recovered(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(target, interrupted)
    _at_a_terminal(monkeypatch, str(home), str(home))
    assert lifecycle.cmd_cluster_destroy() == 1
    assert not _detached(home)
    assert (home / "destroy-intent.json").exists()

    monkeypatch.setattr(target, recovered)
    assert lifecycle.cmd_cluster_destroy() == 0
    assert _detached(home)


# --- the OS jobs ----------------------------------------------------------------


def test_every_scheduled_job_is_retired_including_pr_flow_and_the_walg_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.host.system import autostart, cron, logs_job, packages_job, pr_flow_job, walg_job

    retired: list[str] = []
    for module, name, label in (
        (cron, "unregister_os_cron", "health probe"),
        (autostart, "unregister_autostart", "autostart"),
        (logs_job, "unregister_logs_job", "logs maintenance"),
        (packages_job, "unregister_packages_job", "packages refresh"),
        (pr_flow_job, "unregister_pr_flow_job", "PR flow"),
        (walg_job, "unregister_walg_job", "WAL-G tick"),
    ):
        monkeypatch.setattr(module, name, lambda label=label: retired.append(label))

    lifecycle._unregister_scheduled_jobs()

    assert retired == [
        "health probe",
        "autostart",
        "logs maintenance",
        "packages refresh",
        "PR flow",
        "WAL-G tick",
    ]


def test_a_failed_job_removal_is_raised_after_the_others_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.host.system import autostart, logs_job

    def fail() -> None:
        raise RuntimeError("launchctl unavailable")

    ran: list[str] = []
    monkeypatch.setattr(autostart, "unregister_autostart", fail)
    monkeypatch.setattr(logs_job, "unregister_logs_job", lambda: ran.append("logs"))

    with pytest.raises(RuntimeError, match=r"autostart \(launchctl unavailable\)"):
        lifecycle._unregister_scheduled_jobs()
    assert ran == ["logs"]


def test_destroying_a_scratch_home_never_reaches_the_hosts_scheduler(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Labels name a job, not a home: only the default home may remove one."""

    class _Backend:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"{name} reached the scheduler from a scratch home")

    monkeypatch.setattr("base.host.system.backend.get_backend", _Backend)

    lifecycle._unregister_scheduled_jobs()  # no AssertionError == nothing was touched


def test_cron_cli_keeps_its_interval_and_passes_a_live_owned_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import Callable

    from base.config import ConfigBoot
    from cli.commands.cluster import cron as command

    owner = ConfigBoot()
    owner.set_field("os_jobs_enabled", False)
    observed: list[tuple[int, Callable[[], bool]]] = []

    def register(*, interval_s: int, enabled_reader: Callable[[], bool]) -> None:
        observed.append((interval_s, enabled_reader))

    monkeypatch.setattr(command, "ConfigBoot", lambda: owner)
    monkeypatch.setattr(command, "register_os_cron", register)
    assert command.cmd_cron_register(interval_s=420) == 0
    interval, gate = observed[0]
    assert interval == 420 and gate() is False
    owner.set_field("os_jobs_enabled", True)
    assert gate() is True


def test_cron_cli_keeps_registration_failure_returncode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.commands.cluster import cron as command

    def failed(**_kwargs: object) -> None:
        raise RuntimeError("scheduler denied")

    monkeypatch.setattr(command, "register_os_cron", failed)
    assert command.cmd_cron_register() == 1
    assert capsys.readouterr().out == "  * scheduler denied\n"
