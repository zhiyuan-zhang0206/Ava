"""The pre-stop start-readiness gate (task #3156; restart caller #3165).

`ava start`'s read-only local checks are moved in front of the stop: a failure
HERE refuses the stop while the host still serves, instead of failing on a
stopped host. These tests pin each check family's disposition — fatal (refuse)
vs observation (report, proceed) — plus the two caller-shaped knobs: the
`.venv` entry-point checks (`check_launcher` toggles the one only an update leg
needs). The caller-level refusal contracts live in
`test_update_agent_runner_preflight.py` (update leg) and
`test_commands.py::test_cmd_restart_aborts_when_start_readiness_fails`.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.commands import _start_readiness_preflight as preflight


class _Backend:
    """The POSIX-shaped venv layout stays `<root>/.venv/bin/<name>` under test —
    never this worktree's real venv."""

    def venv_launcher(self, name: str, root: Path) -> Path:
        return root / ".venv" / "bin" / name


@pytest.fixture(autouse=True)
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.platform_backend.get_backend", _Backend)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A unit home with the three private trees, and the environment-dependent
    checks stubbed so each test drives exactly the family it names."""
    home = tmp_path / "home"
    for name in ("logs", "workspaces", "memory"):
        (home / name).mkdir(parents=True)
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)
    monkeypatch.setattr(
        "cli.commands._setup._collect_setup_values",
        lambda _args: ({"machine_role": "agent-runner"}, []),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr("cli.commands._launch_roster", lambda *_a, **_k: ())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        "cli.commands._port_preflight.collect_port_conflicts",
        lambda _ctx: [],  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr("shared.migrations.unreadable_migration_files", list)
    return home


def _venv(
    tmp_path: Path,
    *,
    python: bool = True,
    python_executable: bool = True,
    ava: bool = False,
    ava_executable: bool = True,
) -> Path:
    """A checkout carrying the requested entry-point layout under `.venv/bin/`."""
    repo = tmp_path / "repo"
    for name, present, executable in (
        ("python", python, python_executable),
        ("ava", ava, ava_executable),
    ):
        if not present:
            continue
        entry = repo / ".venv" / "bin" / name
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("#!/bin/sh\n")
        entry.chmod(0o755 if executable else 0o644)
    return repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """The healthy baseline every non-entry-point test starts from: a checkout
    whose `.venv/bin/python` is executable."""
    return _venv(tmp_path)


def _run(repo: Path, *, check_launcher: bool = True) -> int:
    return preflight.preflight_start_readiness(repo, check_launcher=check_launcher)


def test_passes_on_a_clean_home(home: Path, repo: Path) -> None:
    """Every check passes: rc 0, and the report says so."""
    assert _run(repo) == 0


def test_machine_role_missing_skips_ports_as_an_observation(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unresolvable identity is the probe gate's refusal, not this one's: the
    port checks are skipped and the update still proceeds."""
    monkeypatch.setattr(
        "cli.commands._setup._collect_setup_values",
        lambda _args: ({}, ["machine_role"]),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _run(repo) == 0


def test_prod_checkout_violation_is_fatal(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ava start` refuses a prod home launched from a dev checkout; that refusal
    must be reachable before the stop."""
    monkeypatch.setattr(
        "shared.paths.prod_service_checkout_error",
        lambda _repo: "prod home launched from a disposable checkout",  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _run(repo) == 1
    assert "prod home launched from a disposable checkout" in capsys.readouterr().err


def test_health_port_occupancy_is_fatal(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A terminal occupant on a daemon health port refuses `ava start` (#977);
    the same verdict must refuse the stop."""
    occupied = SimpleNamespace(
        spec=SimpleNamespace(session="ava-gateway"),
        detail="answered by $AVA_HOME=/other-unit (http://127.0.0.1:8123/healthz)",
    )
    monkeypatch.setattr("cli.commands._occupied_health_ports", lambda _roster: (occupied,))  # pyright: ignore[reportUnknownArgumentType]

    assert _run(repo) == 1
    err = capsys.readouterr().err
    assert "ava-gateway" in err
    assert "other-unit" in err
    assert "#977" in err


def test_private_tree_root_symlink_is_fatal(
    home: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `logs` root converge would abort on, checked without converging it."""
    (home / "logs").rmdir()
    (home / "logs").symlink_to(repo)

    assert _run(repo) == 1
    err = capsys.readouterr().err
    assert "is a symlink" in err
    assert str(home / "logs") in err


def test_private_tree_root_file_is_fatal(
    home: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (home / "workspaces").rmdir()
    (home / "workspaces").write_text("not a directory")

    assert _run(repo) == 1
    assert "is not a directory" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="FIFO nodes are POSIX-only")
def test_metadata_marker_must_be_a_regular_file(
    home: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The marker converge touches and then repairs: a FIFO there aborts the
    marker write, so it refuses the stop."""
    os.mkfifo(home / "logs" / ".metadata_never_index")

    assert _run(repo) == 1
    assert "is not a regular file" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="FIFO nodes are POSIX-only")
def test_non_regular_node_inside_a_tree_is_an_observation(
    home: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The 2026-09-12 class: converge skips these, so they cannot fail a start —
    but they are reported before the stop instead of only in the converge log."""
    node = home / "workspaces" / "leftover.sock"
    os.mkfifo(node)

    assert _run(repo) == 0
    captured = capsys.readouterr()
    assert str(node) in captured.out
    assert "skips" in captured.out


def test_scan_failure_is_an_observation(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preflight must never be the thing that takes an update down: a scan
    that cannot run is reported and the stop proceeds."""

    def _boom(_root: Path) -> list[Path]:
        raise OSError("permission denied")

    monkeypatch.setattr(preflight, "scan_non_regular_nodes", _boom)

    assert _run(repo) == 0


def test_migration_readability_is_fatal(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The apply-side vet: a tracked migration the applier cannot open would fail
    `ava start`'s apply on the stopped host."""
    monkeypatch.setattr(
        "shared.migrations.unreadable_migration_files",
        lambda: [("20260912T010000_x", "Permission denied: 'x.sql'")],
    )

    assert _run(repo) == 1
    err = capsys.readouterr().err
    assert "20260912T010000_x" in err
    assert "not readable" in err


def test_migration_enumeration_failure_is_fatal(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An enumeration the loader itself refuses (not a git worktree) also fails
    the apply; it must refuse here, not surprise the stopped host."""
    from shared.migrations import MigrationLayoutError

    def _raise() -> list[tuple[str, str]]:
        raise MigrationLayoutError("migrations dir does not exist: /x/migrations")

    monkeypatch.setattr("shared.migrations.unreadable_migration_files", _raise)

    assert _run(repo) == 1
    assert "cannot be enumerated" in capsys.readouterr().err


def test_missing_venv_interpreter_is_fatal(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Task #3165 (D4): every service session launches through
    `.venv/bin/python` — a damaged venv is the restart verb's real
    "stopped and cannot come back" class, and nothing else checks it there."""
    repo = _venv(tmp_path, python=False)

    assert _run(repo) == 1
    err = capsys.readouterr().err
    assert str(repo / ".venv" / "bin" / "python") in err
    assert "is missing" in err


@pytest.mark.skipif(os.name == "nt", reason="exec bits are POSIX-only")
def test_venv_interpreter_without_exec_bit_is_fatal(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _venv(tmp_path, python_executable=False)

    assert _run(repo) == 1
    assert "is not executable" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="exec bits are POSIX-only")
def test_launcher_check_covers_the_ava_entry_point_only_for_the_update_leg(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The `.venv/bin/ava` exec bit is checked when the caller's start execs it
    (the update leg) and skipped for an in-process start (`ava restart`), whose
    sessions never touch it — refusing a bounce over an entry point it does not
    use would block a viable restart."""
    repo = _venv(tmp_path, ava=True, ava_executable=False)
    launcher = repo / ".venv" / "bin" / "ava"

    assert _run(repo) == 1
    assert "not executable" in capsys.readouterr().err

    assert _run(repo, check_launcher=False) == 0

    launcher.chmod(0o755)
    assert _run(repo) == 0


def test_missing_interpreter_still_refuses_an_in_process_start(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`check_launcher=False` only drops the `ava` entry point: the interpreter
    every session launches through is checked for both callers."""
    repo = _venv(tmp_path, python=False, ava=True)

    assert _run(repo, check_launcher=False) == 1
    assert "is missing" in capsys.readouterr().err


def test_all_fatal_findings_are_reported_together(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One refusal lists every reason, so the operator fixes the set in one pass."""
    monkeypatch.setattr(
        "shared.paths.prod_service_checkout_error",
        lambda _repo: "checkout problem",  # pyright: ignore[reportUnknownArgumentType]
    )
    (home / "memory").rmdir()
    (home / "memory").write_text("not a directory")

    assert _run(repo) == 1
    err = capsys.readouterr().err
    assert "checkout problem" in err
    assert str(home / "memory") in err
