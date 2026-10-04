"""Tests for scripts/ci/no_silent_resurrection.py — the 30-day resurrection check.

The behavior this file locks in (decision 2026-10-04, conventions/
no-silent-resurrection.md): the lines a PR adds are compared with the lines
main deleted in the last N days; a run of at least two dead strong lines (or
one dead distinctive line) fails unless a commit in the PR carries a
``Resurrects:`` declaration or reverts the deleting commit. Lines still
present in the base tree, weak lines, single non-distinctive lines, and
skipped generated/lock paths never fail on their own.

Unit tests build throwaway git repositories (committer dates pick the scan
window); the last test replays the real #4207 incident against this
repository's history and skips on shallow checkouts where the commits are
absent.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "no_silent_resurrection.py"

_MOD_NAME = "ci_no_silent_resurrection_under_test"
_spec = importlib.util.spec_from_file_location(_MOD_NAME, _SCRIPT)
assert _spec and _spec.loader
resurrection = importlib.util.module_from_spec(_spec)
sys.modules[_MOD_NAME] = resurrection
_spec.loader.exec_module(resurrection)

_BLOCK_A = (
    'delivery_watchdog_fields = ("last_error", "last_success")\n'
    "delivery_watchdog_alert_grace_seconds = compute_delivery_grace(config)\n"
)
_BLOCK_B = (
    "scheduler_lease_seconds = renew_scheduler_lease(previous_lease)\n"
    "scheduler_lease_owner = resolve_lease_owner(state)\n"
)
_KEEP = "def handle(payload):\n    return payload\n"
_DISTINCTIVE = "AVA_DELIVERY_WATCHDOG_ALERT_GRACE_SECONDS = recompute_grace(default_config)\n"
_GENERIC_LINE = "result = compute(total, rate)\n"


def _env(days_ago: float | None) -> dict[str, str]:
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    if days_ago is not None:
        stamp = (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
    return env


def _git(repo: Path, *args: str, days_ago: float | None = None) -> str:
    completed = subprocess.run(  # noqa: S603
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        env=_env(days_ago),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    return path


def _write(repo: Path, rel: str, content: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _commit(repo: Path, message: str, days_ago: float = 1.0, *, allow_empty: bool = False) -> str:
    _git(repo, "add", "-A")
    args = ["commit", "-q", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    _git(repo, *args, days_ago=days_ago)
    return _git(repo, "rev-parse", "HEAD").strip()


def _check(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    base: str = "main",
    head: str = "HEAD",
    days: int = 30,
) -> tuple[int, str, str]:
    monkeypatch.chdir(repo)
    code = resurrection.main(["--base", base, "--head", head, "--days", str(days)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _restore_after_delete(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    file: str = "a.py",
    block: str = _BLOCK_A,
    trailer: str | None = None,
    add_days_ago: float = 10.0,
    delete_days_ago: float = 2.0,
) -> tuple[int, str, str, str]:
    """Main adds the block, deletes it, and a feature branch restores it."""
    _write(repo, file, "def handle(payload):\n" + block + "    return payload\n")
    _commit(repo, "add the block", days_ago=add_days_ago)
    _write(repo, file, _KEEP)
    deleting = _commit(repo, "remove the block", days_ago=delete_days_ago)
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, file, "def handle(payload):\n" + block + "    return payload\n")
    message = trailer if trailer is not None else "restore the block"
    _commit(repo, message, days_ago=0.1)
    return (*_check(repo, monkeypatch, capsys), deleting)


def test_restored_deleted_block_is_a_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    code, out, _err, deleting = _restore_after_delete(repo, monkeypatch, capsys)
    assert code == 1
    assert "HIT a.py" in out
    assert f"deleted by {deleting[:9]}" in out
    assert "Resurrects:" in out  # the failing output carries the allowance recipe


def test_unrelated_sha_declaration_does_not_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    code, out, _err, deleting = _restore_after_delete(
        repo, monkeypatch, capsys, trailer="Restore the block\n\nResurrects: " + "0" * 39 + "f"
    )
    assert code == 1  # the declaration names an unrelated sha: nothing allowed
    assert f"deleted by {deleting[:9]}" in out
    assert "ALLOWED" not in out


def test_moved_block_in_main_is_not_a_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stale branch cannot hide a move: the text still exists in the base tree."""
    repo = _init_repo(tmp_path / "repo")
    _write(repo, "a.py", "def handle(payload):\n" + _BLOCK_A + "    return payload\n")
    _commit(repo, "add the block", days_ago=10)
    _write(repo, "a.py", _KEEP)
    _write(repo, "b.py", _BLOCK_A)
    _commit(repo, "move the block to b.py", days_ago=2)
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "c.py", _BLOCK_A)
    _commit(repo, "copy the same lines again", days_ago=0.1)
    code, out, _err = _check(repo, monkeypatch, capsys)
    assert code == 0
    assert "no resurrection detected" in out


def test_same_commit_move_is_not_a_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A commit that removes the lines and re-adds the text (a move) deleted nothing.

    The re-add lands in a skipped path, where the alive grep does not look:
    only the same-commit subtraction can tell this apart from a deletion.
    """
    repo = _init_repo(tmp_path / "repo")
    _write(repo, "a.py", "def handle(payload):\n" + _BLOCK_A + "    return payload\n")
    _commit(repo, "add the block", days_ago=10)
    _write(repo, "a.py", _KEEP)
    _write(repo, "uv.lock", _BLOCK_A)
    _commit(repo, "move the block into the lockfile", days_ago=2)
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "c.py", _BLOCK_A)
    _commit(repo, "the block appears again", days_ago=0.1)
    code, out, _err = _check(repo, monkeypatch, capsys)
    assert code == 0
    assert "no resurrection detected" in out


def test_line_present_elsewhere_in_base_is_not_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    line = "delivery_watchdog_fields = compute_fields(config)\n"
    _write(repo, "a.py", line)
    _write(repo, "b.py", line)
    _commit(repo, "two copies", days_ago=10)
    _write(repo, "b.py", "def other():\n    return None\n")
    _commit(repo, "delete one copy", days_ago=2)
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "c.py", line)
    _commit(repo, "add the line again", days_ago=0.1)
    code, out, _err = _check(repo, monkeypatch, capsys)
    assert code == 0
    assert "still in the base tree" in out


def test_skipped_path_is_never_a_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write(repo, "uv.lock", _BLOCK_A)
    _commit(repo, "lockfile with the lines", days_ago=10)
    _write(repo, "uv.lock", "")
    _commit(repo, "regenerate the lockfile", days_ago=2)
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "uv.lock", _BLOCK_A)
    _commit(repo, "restore the lockfile", days_ago=0.1)
    code, out, _err = _check(repo, monkeypatch, capsys)
    assert code == 0
    assert "no resurrection detected" in out


def test_single_distinctive_line_is_a_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write(repo, "a.py", _DISTINCTIVE)
    _commit(repo, "add the constant", days_ago=10)
    _write(repo, "a.py", "def handle(payload):\n    return payload\n")
    deleting = _commit(repo, "remove the constant", days_ago=2)
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "a.py", _DISTINCTIVE)
    _commit(repo, "restore the constant", days_ago=0.1)
    code, out, _err = _check(repo, monkeypatch, capsys)
    assert code == 1
    assert f"deleted by {deleting[:9]}" in out


def test_single_generic_line_is_not_a_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    body = "def handle(payload):\n    " + _GENERIC_LINE + "    return payload\n"
    _write(repo, "a.py", body)
    _commit(repo, "add the body", days_ago=10)
    _write(repo, "a.py", _KEEP)
    _commit(repo, "remove the line", days_ago=2)
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "a.py", body)
    _commit(repo, "restore the line", days_ago=0.1)
    code, out, _err = _check(repo, monkeypatch, capsys)
    assert code == 0
    assert "no resurrection detected" in out


def test_weak_lines_are_only_glue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    weak = "# module note about the delivery watchdog\nx = 1\nimport os\n\n"
    _write(repo, "a.py", weak)
    _commit(repo, "add the weak lines", days_ago=10)
    _write(repo, "a.py", "")
    _commit(repo, "remove the weak lines", days_ago=2)
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "a.py", weak)
    _commit(repo, "re-add the weak lines", days_ago=0.1)
    code, out, _err = _check(repo, monkeypatch, capsys)
    assert code == 0
    assert "no resurrection detected" in out


def test_resurrects_line_allows_every_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    code, out, _err, _deleting = _restore_after_delete(
        repo,
        monkeypatch,
        capsys,
        trailer="restore the block\n\nResurrects: the watchdog fields are restored deliberately",
    )
    assert code == 0
    assert "ALLOWED a.py" in out
    assert "all allowed" in out


def test_resurrects_sha_allows_only_that_commit_s_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write(repo, "a.py", "def handle(payload):\n" + _BLOCK_A + "    return payload\n")
    _write(repo, "b.py", "def handle(payload):\n" + _BLOCK_B + "    return payload\n")
    _commit(repo, "add both blocks", days_ago=10)
    _write(repo, "a.py", _KEEP)
    first = _commit(repo, "remove block A", days_ago=2)
    _write(repo, "b.py", _KEEP)
    _commit(repo, "remove block B", days_ago=1.5)
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "a.py", "def handle(payload):\n" + _BLOCK_A + "    return payload\n")
    _write(repo, "b.py", "def handle(payload):\n" + _BLOCK_B + "    return payload\n")
    _commit(repo, "restore both\n\nResurrects: " + first[:9], days_ago=0.1)
    code, out, err = _check(repo, monkeypatch, capsys)
    assert code == 1
    assert "ALLOWED a.py" in out
    assert "HIT b.py" in out
    assert "matched no hit" not in err  # the declaration was used


def test_resurrects_path_allows_only_matching_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write(repo, "pkg/a.py", "def handle(payload):\n" + _BLOCK_A + "    return payload\n")
    _write(repo, "b.py", "def handle(payload):\n" + _BLOCK_B + "    return payload\n")
    _commit(repo, "add both blocks", days_ago=10)
    _write(repo, "pkg/a.py", _KEEP)
    _commit(repo, "remove block A", days_ago=2)
    _write(repo, "b.py", _KEEP)
    _commit(repo, "remove block B", days_ago=1.5)
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "pkg/a.py", "def handle(payload):\n" + _BLOCK_A + "    return payload\n")
    _write(repo, "b.py", "def handle(payload):\n" + _BLOCK_B + "    return payload\n")
    _commit(repo, "restore both\n\nResurrects: pkg/", days_ago=0.1)
    code, out, _err = _check(repo, monkeypatch, capsys)
    assert code == 1
    assert "ALLOWED pkg/a.py" in out
    assert "HIT b.py" in out
    assert "HIT pkg/a.py" not in out


def test_revert_message_allows_that_commit_s_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    code, out, _err, deleting = _restore_after_delete(repo, monkeypatch, capsys)
    assert code == 1  # precondition: without the trailer the check fails
    _git(repo, "checkout", "-q", "feature")
    _commit(
        repo,
        'Revert "remove the block"\n\nThis reverts commit ' + deleting + ".",
        days_ago=0.05,
        allow_empty=True,
    )
    code, out, _err = _check(repo, monkeypatch, capsys)
    assert code == 0
    assert "ALLOWED a.py" in out


def test_revert_of_another_commit_does_not_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write(repo, "seed.py", "def seed_config(registry):\n    return registry\n")
    other = _commit(repo, "seed", days_ago=10)
    code, _out, _err, _deleting = _restore_after_delete(repo, monkeypatch, capsys)
    _git(repo, "checkout", "-q", "feature")
    _commit(repo, "This reverts commit " + other + ".", days_ago=0.05, allow_empty=True)
    code, _out, err = _check(repo, monkeypatch, capsys)
    assert code == 1
    assert "matched no hit" in err  # the revert declaration allowed nothing


def test_unused_resurrects_declaration_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write(repo, "a.py", _KEEP)
    _commit(repo, "initial", days_ago=10)
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "b.py", "def extra(registry):\n    return registry\n")
    _commit(repo, "add code\n\nResurrects: totally/unrelated/path.py", days_ago=0.1)
    code, out, err = _check(repo, monkeypatch, capsys)
    assert code == 0
    assert "no resurrection detected" in out
    assert "matched no hit" in err


def test_delete_older_than_the_window_is_not_a_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    code, _out, _err, _deleting = _restore_after_delete(
        repo, monkeypatch, capsys, add_days_ago=50.0, delete_days_ago=40.0
    )
    assert code == 0  # outside the default 30-day window
    monkeypatch.chdir(repo)
    assert resurrection.main(["--base", "main", "--head", "HEAD", "--days", "60"]) == 1
    out = capsys.readouterr().out
    assert "HIT a.py" in out


def test_unknown_revision_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write(repo, "a.py", _KEEP)
    _commit(repo, "initial", days_ago=1)
    monkeypatch.chdir(repo)
    code = resurrection.main(["--base", "no-such-ref", "--head", "HEAD"])
    captured = capsys.readouterr()
    assert code == 2
    assert "not a commit" in captured.err


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", (False, False)),
        ('"""', (False, False)),
        (")", (False, False)),
        ("return None", (False, False)),
        ("import delivery_watchdog_fields", (False, False)),
        ("from config import delivery_watchdog_fields", (False, False)),
        ("# delivery_watchdog_fields = compute_fields(config)", (False, False)),
        ("x = 1", (False, False)),
        ("delivery_watchdog_fields = " + "x" * 1100, (False, False)),  # data blob
        ("result = compute(total, rate)", (True, False)),
        ("delivery_watchdog_fields = compute_fields(config)", (True, True)),
        ("AVA_DELIVERY_WATCHDOG_ALERT_GRACE_SECONDS = recompute_grace(cfg)", (True, True)),
    ],
)
def test_line_classification(text: str, expected: tuple[bool, bool]) -> None:
    assert resurrection._classify(text) == expected


@pytest.mark.parametrize(
    ("reason", "shas", "paths"),
    [
        ("restore the block deliberately", [], []),
        ("restore 6ecfe14c3 deliberately", ["6ecfe14c3"], []),
        ("restore R6ECfe14c3", [], []),  # a path-less token that is not all hex
        ("restore pkg/a.py", [], ["pkg/a.py"]),
        ("restore decisions/", [], ["decisions/"]),
        ("restore README.md", [], ["README.md"]),
        ("restore version v1.24.5", [], []),  # a version is not a file path
    ],
)
def test_declaration_parsing(reason: str, shas: list[str], paths: list[str]) -> None:
    assert resurrection._declared_targets(reason) == (shas, paths)


def _git_ok(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", *args], cwd=_REPO_ROOT, capture_output=True, text=True, check=False
    )


def test_recent_main_incident_is_detected() -> None:
    """The real #4207 replay (151bc92fe) must fail the check (decision 2026-10-04).

    Skips on shallow checkouts or when either commit is missing, so the test
    rides along wherever the full history is available.
    """
    if _git_ok("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        pytest.skip("shallow checkout: the incident commits are not available")
    for sha in ("151bc92fe", "6ecfe14c3"):
        if _git_ok("cat-file", "-e", sha + "^{commit}").returncode != 0:
            pytest.skip(f"commit {sha} is not in this checkout")
    timestamp = _git_ok("show", "-s", "--format=%ct", "6ecfe14c3").stdout.strip()
    days = (datetime.now(UTC) - datetime.fromtimestamp(int(timestamp), UTC)).days + 7
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(_SCRIPT),
            "--base",
            "151bc92fe^",
            "--head",
            "151bc92fe",
            "--days",
            str(days),
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "6ecfe14c3" in completed.stdout
    assert "delivery_watchdog_fields" in completed.stdout
