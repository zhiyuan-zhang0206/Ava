"""scripts/lint_core_content_manifests.py — the core-content manifest gate.

Hard checks (red): a core manifest that fails validation, whose `engines.ava`
excludes the repo's derived host version, or whose `requires_commit` is not an
ancestor of HEAD. Audit (report-only): non-core manifests are judged against
the derived + legacy host versions without failing the run.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import lint_core_content_manifests as lint

_COMMIT_DATE = "2026-01-02T12:00:00+00:00"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch repo root with one commit (derived version 2026.1.2)."""
    monkeypatch.setattr(lint, "_REPO_ROOT", tmp_path)
    subprocess.run(  # noqa: S603 — fixed argv, test-local fixture repo
        ["git", "init", "-q", "-b", "main", str(tmp_path)], check=True, capture_output=True
    )
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(  # noqa: S603 — fixed argv, test-local fixture repo
        ["git", "-C", str(tmp_path), "add", "-A"], check=True, capture_output=True
    )
    env = os.environ | {"GIT_AUTHOR_DATE": _COMMIT_DATE, "GIT_COMMITTER_DATE": _COMMIT_DATE}
    subprocess.run(  # noqa: S603 — fixed argv, test-local fixture repo
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "-m",
            "one",
        ],
        check=True,
        capture_output=True,
        env=env,
    )
    return tmp_path


def _head(repo: Path) -> str:
    return subprocess.run(  # noqa: S603 — fixed argv, test-local fixture repo
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write(repo: Path, rel: str, payload: dict[str, object]) -> None:
    """Write + STAGE a manifest (the scan reads `git ls-files`)."""
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")
    subprocess.run(  # noqa: S603 — fixed argv, test-local fixture repo
        ["git", "-C", str(repo), "add", rel], check=True, capture_output=True
    )


def test_derived_version_resolves_from_fixture_commit(repo: Path) -> None:
    from shared import host_version

    assert host_version.host_version(repo) == "2026.1.2"


def test_valid_core_manifest_passes(repo: Path) -> None:
    _write(
        repo,
        "ava_builtins/skills/foo/ava-plugin.json",
        {"apiVersion": 2, "name": "foo", "version": "1.0.0", "engines": {"ava": ">=2026.1.1"}},
    )
    assert lint.main([]) == 0


def test_core_manifest_excluding_current_version_is_red(repo: Path) -> None:
    _write(
        repo,
        "ava_builtins/skills/foo/ava-plugin.json",
        {"apiVersion": 2, "name": "foo", "version": "1.0.0", "engines": {"ava": ">=0.1.0,<1"}},
    )
    assert lint.main([]) == 1


def test_core_manifest_invalid_is_red(repo: Path) -> None:
    _write(repo, "ava_builtins/skills/foo/ava-plugin.json", {"apiVersion": 2})
    assert lint.main([]) == 1


def test_core_requires_commit_ancestor_passes(repo: Path) -> None:
    _write(
        repo,
        "ava_builtins/skills/foo/ava-plugin.json",
        {
            "apiVersion": 2,
            "name": "foo",
            "version": "1.0.0",
            "requires_commit": _head(repo),
        },
    )
    assert lint.main([]) == 0


def test_core_requires_commit_unresolvable_is_red(repo: Path) -> None:
    _write(
        repo,
        "ava_builtins/skills/foo/ava-plugin.json",
        {"apiVersion": 2, "name": "foo", "version": "1.0.0", "requires_commit": "f" * 40},
    )
    assert lint.main([]) == 1


def test_non_core_manifest_is_audited_not_failed(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A legacy third-party/fixture manifest outside ava_builtins gets the
    derived-vs-legacy verdict printed without reddening the run."""
    _write(
        repo,
        "examples/thing/ava-plugin.json",
        {"apiVersion": 2, "name": "thing", "version": "1.0.0", "engines": {"ava": "<1"}},
    )
    assert lint.main([]) == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "audit examples/thing/ava-plugin.json" in out
    assert "derived 2026.1.2: FAIL" in out
