"""The standalone `--publish-offsite` entry: success and failure both leave evidence on stderr.

Each case runs `services.backup.dump.run_offsite_publisher` in a fresh interpreter (`child_offsite_entry.py`)
against the in-memory OSS fake of `services/backup/artifact/tests/oss_fake.py`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from services.backup.artifact.names import REMOTE_ROOT

_CHILD = Path(__file__).with_name("child_offsite_entry.py")
_NAME = "meiva-20260916T030000Z.dump.enc"


def _artifact(tmp_path: Path, data: bytes = b"encrypted artifact") -> Path:
    path = tmp_path / _NAME
    path.write_bytes(data)
    return path


def _entry(
    tmp_path: Path, bucket: str, *args: str, configured: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run `python -m services.backup.dump --publish-offsite` logic in a fresh interpreter.

    A fresh interpreter is deliberate: pytest's own root handler would mask the
    standalone logging behavior the entry point configures. `configured` gives
    it an OSS destination (the settings read is covered in-process above);
    `bucket` is `fake`, `init_failure` or `real` (see `child_offsite_entry.py`)."""
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(_CHILD),
            "configured" if configured else "unconfigured",
            bucket,
            "--publish-offsite",
            str(tmp_path / _NAME),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_standalone_success_is_visible(tmp_path: Path) -> None:
    """A fully silent success was once read as a dead upload (misdiagnosed 2026-09-16)."""
    _artifact(tmp_path)

    proc = _entry(
        tmp_path,
        "fake",
        "--offsite-root",
        "meiva-pitr-scratch/smoke",
    )

    assert proc.returncode == 0, proc.stderr
    assert "[backup] off-site published" in proc.stderr
    assert f"meiva-pitr-scratch/smoke/{_NAME}" in proc.stderr


def test_standalone_unconfigured_still_says_so_and_exits_zero(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)

    proc = _entry(tmp_path, "real", configured=False)

    assert proc.returncode == 0, proc.stderr
    assert "[backup] off-site publish skipped" in proc.stderr
    assert artifact.read_bytes() == b"encrypted artifact"


def test_standalone_publish_failure_keeps_exit_and_artifact(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)

    proc = _entry(tmp_path, "init_failure")

    assert proc.returncode == 0, proc.stderr
    assert f"[backup] off-site publish of {REMOTE_ROOT}/{_NAME} failed" in proc.stderr
    assert artifact.read_bytes() == b"encrypted artifact"
