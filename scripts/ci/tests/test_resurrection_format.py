"""CLI contracts for retained keyword syntax and conservative resurrection detection."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "no_silent_resurrection.py"
KEYWORDS = "        reason=notice.reason,\n        operation=notice.operation,\n        acquired_at=notice.acquired_at,\n"
COMPACT = "    result = notify(items, reason=notice.reason, operation=notice.operation, acquired_at=notice.acquired_at)\n"
EXPANDED = "    result = notify(\n        items,\n" + KEYWORDS
BASE = "def handle(items, notice):\n" + COMPACT + "    return result\n"


def git(repo: Path, *args: str, age: int = 1) -> str:
    stamp = (datetime.now(UTC) - timedelta(days=age)).isoformat()
    env = dict(
        os.environ,
        GIT_AUTHOR_DATE=stamp,
        GIT_COMMITTER_DATE=stamp,
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
    )
    return subprocess.check_output(  # noqa: S603 — fixed Git fixture commands
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
        env=env,
        text=True,
    )


def commit(repo: Path, source: str, message: str) -> None:
    (repo / "a.py").write_text(source)
    git(repo, "add", "a.py")
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def history(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    commit(
        tmp_path,
        "def handle(items, notice):\n"
        + EXPANDED.replace("notify(", "old_notify(")
        + "    )\n    return result\n",
        "old operation",
    )
    commit(tmp_path, BASE, "replace old operation")
    return tmp_path


def check(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed checker CLI
        [sys.executable, str(SCRIPT), "--base", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("addition", ["", "        local_machine=machine.name(),\n"])
def test_retained_keyword_lines_do_not_resurrect(history: Path, addition: str) -> None:
    git(history, "checkout", "-q", "-b", "feature")
    commit(
        history,
        "def handle(items, notice):\n" + EXPANDED + addition + "    )\n    return result\n",
        "format retained call",
    )
    result = check(history)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 retained keyword line(s)" in result.stdout
    assert "ALLOWED" not in result.stdout


@pytest.mark.parametrize(
    "change",
    [
        "callee",
        "copy",
        "guard",
        "unpack",
        "invalid",
        "reorder",
        "duplicate_owner",
    ],
)
def test_unproven_correspondence_keeps_raw_hit(history: Path, change: str) -> None:
    source = "def handle(items, notice):\n" + EXPANDED + "    )\n    return result\n"
    if change == "callee":
        source = source.replace("notify(", "old_notify(")
    elif change == "copy":
        source = BASE + "\ndef copied(items, notice):\n" + EXPANDED + "    )\n    return result\n"
    elif change == "guard":
        source = (
            "def handle(items, notice):\n    if safe:\n"
            + "\n".join("    " + s for s in (EXPANDED + "    )\n").splitlines())
            + "\n"
        )
    elif change == "unpack":
        source = source.replace("    )", "        **extra,\n    )")
    elif change == "invalid":
        source += "invalid = (\n"
    elif change == "reorder":
        source = source.replace(
            KEYWORDS,
            "        operation=notice.operation,\n        reason=notice.reason,\n        acquired_at=notice.acquired_at,\n",
        )
    elif change == "duplicate_owner":
        source = BASE + "\n" + source
    git(history, "checkout", "-q", "-b", "feature")
    commit(history, source, "restore historical fragments without proof")
    result = check(history)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "HIT a.py" in result.stdout
    assert "0 retained keyword line(s)" in result.stdout
    if change == "invalid":
        assert "unresolved syntax" in result.stdout


@pytest.mark.parametrize("changed", ["", "        protection=two,\n"])
def test_deleted_or_changed_original_input_keeps_raw_hit(history: Path, changed: str) -> None:
    commit(
        history,
        BASE.replace(
            "acquired_at=notice.acquired_at)", "acquired_at=notice.acquired_at, protection=one)"
        ),
        "extra input",
    )
    git(history, "checkout", "-q", "-b", "feature")
    commit(history, "def handle(items, notice):\n" + EXPANDED + changed + "    )\n", "alter input")
    result = check(history)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "HIT a.py" in result.stdout
    assert "0 retained keyword line(s)" in result.stdout


def test_retained_line_bridges_three_restored_operations(history: Path) -> None:
    extra = "        policy=resolve_policy(config),\n"
    extra += "        owner=resolve_owner(config),\n"
    extra += "        fields=resolve_fields(config),\n"
    commit(
        history, "def handle(items, notice):\n" + EXPANDED + extra + "    )\n", "historical inputs"
    )
    commit(history, BASE, "delete inputs")
    git(history, "checkout", "-q", "-b", "feature")
    mixed = "".join(
        a + b
        for a, b in zip(
            KEYWORDS.splitlines(keepends=True), extra.splitlines(keepends=True), strict=True
        )
    )
    commit(
        history,
        "def handle(items, notice):\n    result = notify(\n        items,\n" + mixed + "    )\n",
        "format and restore inputs",
    )
    result = check(history)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "3 retained keyword line(s)" in result.stdout
    # The historical call opener is also dead; retained fields must bridge it.
    assert "4 dead line(s)" in result.stdout


def test_deleted_call_has_no_retained_site(history: Path) -> None:
    commit(history, "def handle(items, notice):\n    return items\n", "remove call")
    git(history, "checkout", "-q", "-b", "feature")
    commit(
        history,
        "def handle(items, notice):\n" + EXPANDED + "    )\n    return result\n",
        "restore operation",
    )
    result = check(history)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "0 retained keyword line(s)" in result.stdout


def test_real_machine_diff_only_retains_existing_inputs() -> None:
    base = "a80392585215c23f2d0a7ddd96807abb3a422dd8"
    head = "4549e170ac76cfa9a905924c5b09c7a06db594cf"
    repo = SCRIPT.parents[2]
    for rev in (base, head):
        if subprocess.run(  # noqa: S603 — fixed commit-object query
            ["git", "cat-file", "-e", rev], cwd=repo, capture_output=True, check=False
        ).returncode:
            pytest.skip("fixed Machine history absent from checkout")
    result = subprocess.run(  # noqa: S603 — fixed history replay
        [sys.executable, str(SCRIPT), "--base", base, "--head", head],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 retained keyword line(s)" in result.stdout
    assert "ALLOWED" not in result.stdout
