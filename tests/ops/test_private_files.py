"""Tests for the private production-file registry verifier."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from ops.private_files import verify

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RELATIVE_PATH = Path("ops/private/example.txt")
_CONTENT = b"private production source\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "production"
    source_repo = tmp_path / "archive"
    checked_file = root / _RELATIVE_PATH
    source_file = source_repo / _RELATIVE_PATH
    checked_file.parent.mkdir(parents=True)
    source_file.parent.mkdir(parents=True)
    checked_file.write_bytes(_CONTENT)
    source_file.write_bytes(_CONTENT)

    _git(source_repo, "init", "-b", "main")
    _git(source_repo, "config", "user.email", "test@example.com")
    _git(source_repo, "config", "user.name", "Test")
    _git(source_repo, "add", _RELATIVE_PATH.as_posix())
    _git(source_repo, "commit", "-m", "archive private file")

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "id": "example",
                        "path": _RELATIVE_PATH.as_posix(),
                        "source": {
                            "kind": "git",
                            "repo": str(source_repo),
                            "treeish": "HEAD",
                        },
                        "sha256": hashlib.sha256(_CONTENT).hexdigest(),
                        "status": "active",
                        "notes": "Test entry.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return root, source_repo, manifest


def _run_cli(root: Path, manifest: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "ops.private_files",
            "verify",
            "--root",
            str(root),
            "--manifest",
            str(manifest),
            *extra_args,
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_verify_passes_without_modifying_checked_file(tmp_path: Path) -> None:
    root, _source_repo, manifest = _fixture(tmp_path)
    checked_file = root / _RELATIVE_PATH
    original = checked_file.read_bytes()

    outcomes = verify(root, manifest)

    assert len(outcomes) == 1
    assert outcomes[0].ok is True
    assert outcomes[0].errors == ()
    assert checked_file.read_bytes() == original


def test_verify_reports_missing_file(tmp_path: Path) -> None:
    root, _source_repo, manifest = _fixture(tmp_path)
    (root / _RELATIVE_PATH).unlink()

    outcome = verify(root, manifest)[0]

    assert outcome.ok is False
    assert outcome.errors == ("file is missing",)


def test_verify_reports_tampered_file(tmp_path: Path) -> None:
    root, _source_repo, manifest = _fixture(tmp_path)
    (root / _RELATIVE_PATH).write_bytes(b"tampered\n")

    outcome = verify(root, manifest)[0]

    assert outcome.ok is False
    assert len(outcome.errors) == 1
    assert outcome.errors[0].startswith("file sha256 mismatch:")


def test_verify_reports_missing_source(tmp_path: Path) -> None:
    root, source_repo, manifest = _fixture(tmp_path)
    source_repo.rename(tmp_path / "moved-archive")

    outcome = verify(root, manifest)[0]

    assert outcome.ok is False
    assert outcome.errors == (f"source git repository is missing: {source_repo}",)


def test_cli_exit_codes_and_json_output(tmp_path: Path) -> None:
    root, _source_repo, manifest = _fixture(tmp_path)

    passed = _run_cli(root, manifest, "--json")

    assert passed.returncode == 0
    assert json.loads(passed.stdout) == [
        {
            "id": "example",
            "path": _RELATIVE_PATH.as_posix(),
            "ok": True,
            "errors": [],
        }
    ]

    (root / _RELATIVE_PATH).unlink()
    failed = _run_cli(root, manifest)

    assert failed.returncode == 1
    assert failed.stdout == f"FAIL example {_RELATIVE_PATH.as_posix()}: file is missing\n"
