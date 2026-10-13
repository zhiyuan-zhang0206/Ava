"""The generated pre-update schedule check runs on hosts whose OLD checkout is not this one.

`fleet_update` is run from the operator's (NEW) checkout, but the script it generates executes the
read step under each host's OLD source checkout and the check under a worktree of NEW. The
2026-10 `6a2e57f` -> `3fd41ecbc6` dry run showed both steps leaning on NEW-only API: the read step
imported `cli.database` (absent in OLD) and the check called `cmd_schedules_verify` with a
signature OLD's interpreter never saw. These tests run the real script text against stand-ins for
both sides.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from base.paths import repo_root
from cli import fleet_update

_OLD_ROWS = [(1, "digest", "import os\n"), (2, "empty", "")]


def _tree(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _packages(*dirs: str) -> dict[str, str]:
    return {f"{d}/__init__.py": "" for d in dirs}


# OLD as of `6a2e57f`: no `cli.database`, a read with no parameters.
_OLD_CHECKOUT = {
    **_packages("cli", "cli/commands", "cli/commands/management"),
    "cli/commands/management/schedules_verify.py": (
        f"def _read_schedule_rows():\n    return {_OLD_ROWS!r}\n"
    ),
}
# A later OLD: the read takes the database factory the operator CLI builds.
_FACTORY_CHECKOUT = {
    **_packages("cli", "cli/commands", "cli/commands/management", "cli/database"),
    "cli/commands/management/schedules_verify.py": (
        "def _read_schedule_rows(*, database_factory):\n"
        "    return [(1, 'digest', 'host=' + database_factory())]\n"
    ),
    "cli/database/__init__.py": (
        "def operator_database_factory(*, local_host):\n    return lambda: local_host\n"
    ),
    "cli/database/runtime.py": (
        "class _Machine:\n    host = 'gateway'\n\n\n"
        "class CommandRuntime:\n    machine = _Machine()\n\n"
        "    @classmethod\n    def create(cls):\n        return cls()\n"
    ),
}


def _read_rows(checkout: Path, out: Path) -> subprocess.CompletedProcess[str]:
    """The read step's python, exactly as the script passes it, from inside `checkout`."""
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(checkout), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [
            sys.executable,
            str(fleet_update.VERIFY_SCRIPTS / "schedules_verify_read_rows.py"),
            str(out),
        ],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_the_read_step_runs_under_an_old_checkout_without_cli_database(tmp_path: Path) -> None:
    _tree(tmp_path / "old", _OLD_CHECKOUT)
    assert not (tmp_path / "old/cli/database").exists()
    out = tmp_path / "rows.json"
    proc = _read_rows(tmp_path / "old", out)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(out.read_text()) == [list(row) for row in _OLD_ROWS]


def test_the_read_step_passes_the_operator_factory_when_the_old_read_wants_one(
    tmp_path: Path,
) -> None:
    _tree(tmp_path / "old", _FACTORY_CHECKOUT)
    out = tmp_path / "rows.json"
    proc = _read_rows(tmp_path / "old", out)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(out.read_text()) == [[1, "digest", "host=gateway"]]


def test_a_failing_read_is_an_error_not_a_fallback(tmp_path: Path) -> None:
    broken = {
        **_OLD_CHECKOUT,
        "cli/commands/management/schedules_verify.py": (
            "def _read_schedule_rows():\n    raise RuntimeError('database refused')\n"
        ),
    }
    _tree(tmp_path / "old", broken)
    proc = _read_rows(tmp_path / "old", tmp_path / "rows.json")
    assert proc.returncode != 0 and "database refused" in proc.stderr


def test_the_read_script_imports_nothing_from_cli_database_unconditionally() -> None:
    read = fleet_update.read_rows_py()
    assert read.index("inspect.signature") < read.index("cli.database")
    assert not [
        line
        for line in read.splitlines()
        if line.startswith(("from cli.database", "import cli.database"))
    ]


def _render(new: str = "deadbeef") -> str:
    return fleet_update.PRE_VERIFY.format(
        home=fleet_update.HOME,
        new=new,
        read_rows=shlex.quote(fleet_update.read_rows_py()),
        check_rows=shlex.quote(fleet_update.check_rows_py()),
    )


def test_the_check_goes_through_the_cli_entry_with_a_throwaway_home() -> None:
    script = _render()
    check = next(line for line in script.splitlines() if line.startswith('cd "$W"'))
    assert 'AVA_HOME="$T"' in check and "AVA_CONFIG_FETCH=skip" in check
    assert "mktemp -d" in script and 'rm -rf "$T"' in script
    assert "cmd_schedules_verify" not in script
    assert '(umask 077; AVA_HOME="$H"' in script  # the rows file holds every stored script
    assert 'rm -f "$R"' in script and 'worktree remove --force "$W"' in script
    check_py = fleet_update.check_rows_py()
    assert '"cli.main"' in check_py and '"schedules"' in check_py and '"verify"' in check_py
    assert '"--rows-file"' in check_py and '"--no-notify"' in check_py


def test_the_rendered_script_is_valid_shell() -> None:
    proc = subprocess.run(
        ["bash", "-n"], input=_render(), capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


def test_the_real_cli_checks_a_rows_file_offline_under_a_throwaway_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    rows = tmp_path / "rows.json"
    rows.write_text(
        json.dumps([[1, "clean", "import os\n"], [7, "drifted", "import zz_missing_mod\n"]])
    )
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "AVA_HOME": str(home),
        "AVA_CONFIG_FETCH": "skip",
    }
    proc = subprocess.run(
        [
            sys.executable,
            str(fleet_update.VERIFY_SCRIPTS / "schedules_verify_check_rows.py"),
            str(rows),
        ],
        cwd=repo_root(),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "checked=2 green=1 red=1" in proc.stdout
    assert "RED id=7 name=drifted missing=zz_missing_mod" in proc.stdout


# NEW's CLI stand-in: prints what the check was given and exits with the verdict in the rows file.
_NEW_MAIN = """\
import json, os, sys


def main():
    rows = json.load(open(sys.argv[sys.argv.index("--rows-file") + 1]))
    print("ARGV " + " ".join(sys.argv[1:]))
    print("HOME_IS_THROWAWAY " + str(os.environ["AVA_HOME"] != os.environ["REAL_HOME"]))
    reds = [row for row in rows if "RED" in row[2]]
    for row in reds:
        print(f"RED id={row[0]} name={row[1]} missing=x")
    return 1 if reds else 0


if __name__ == "__main__":
    sys.exit(main())
"""


def _git(repo: Path, *args: str) -> str:
    command = ["git", "-c", "user.name=t", "-c", "user.email=t@x.invalid", "-C", str(repo), *args]
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def host(tmp_path: Path) -> tuple[Path, str]:
    """A `$HOME/.ava/source` at OLD (stub cli, no `cli.database`) plus a NEW commit with a stub CLI."""
    source = tmp_path / ".ava" / "source"
    source.mkdir(parents=True)
    _git(source, "init", "-q", "--initial-branch=main")
    _tree(source, _OLD_CHECKOUT)
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "old")
    _tree(source, {"cli/main.py": _NEW_MAIN})
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "new")
    new = _git(source, "rev-parse", "HEAD")
    _git(source, "reset", "-q", "--hard", "HEAD~1")
    (source / ".venv" / "bin").mkdir(parents=True)
    (source / ".venv" / "bin" / "python").symlink_to(sys.executable)
    _git(source, "config", "status.showUntrackedFiles", "no")
    return tmp_path, new


def _run_script(home: Path, new: str, *, red: bool) -> subprocess.CompletedProcess[str]:
    rows = [(1, "digest", "RED" if red else "import os\n")]
    old_py = home / ".ava/source/cli/commands/management/schedules_verify.py"
    old_py.write_text(f"def _read_schedule_rows():\n    return {rows!r}\n")
    tmp = home / "tmp"
    tmp.mkdir(exist_ok=True)
    env = {**os.environ, "HOME": str(home), "TMPDIR": str(tmp), "REAL_HOME": str(home / ".ava")}
    return subprocess.run(
        ["bash", "-c", _render(new)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_script_checks_the_table_through_new_and_cleans_up(host: tuple[Path, str]) -> None:
    home, new = host
    proc = _run_script(home, new, red=True)
    lines = proc.stdout.splitlines()
    assert proc.returncode == 0, proc.stderr
    assert "VERIFY_RC=1" in lines and "RED id=1 name=digest missing=x" in lines
    assert "ARGV schedules verify --rows-file " in proc.stdout and "--no-notify" in proc.stdout
    assert "HOME_IS_THROWAWAY True" in lines
    assert not (home / ".ava/pre-update-verify").exists()
    assert not (home / ".ava/pre-update-verify.rows").exists()
    assert list((home / "tmp").iterdir()) == []


def test_a_clean_table_gives_rc_zero(host: tuple[Path, str]) -> None:
    home, new = host
    assert "VERIFY_RC=0" in _run_script(home, new, red=False).stdout.splitlines()


def test_a_host_that_has_not_fetched_new_is_reported_not_checked(host: tuple[Path, str]) -> None:
    home, _ = host
    proc = _run_script(home, "0" * 40, red=False)
    assert proc.returncode == 0
    assert proc.stdout.startswith("SKIPPED") and "VERIFY_RC" not in proc.stdout
