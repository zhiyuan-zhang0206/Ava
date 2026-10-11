"""The scheduler daemon must not carry the OSS SDK; the backup worker still loads it.

The daemon (`services.backup.scheduler.daemon`) imports `services.backup.dump` for
`is_due` and the cluster clock. The dump itself runs in the operation worker's
subprocess, and only that subprocess publishes off-site. An eager
`import oss2` in the daemon's import chain keeps `oss2`, `aliyunsdkcore`,
`cryptography` and `requests` resident in a process that never uploads
anything (16 MiB of private memory). Each test runs in a fresh interpreter with
a throwaway home, the way the roster launches the service.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SDK_ROOTS = ("oss2", "aliyunsdkcore")

_PROBE = Path(__file__).with_name("child_import_probe.py")


def _fresh(args: list[str], home: Path) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env.update(AVA_HOME=str(home), AVA_CONFIG_FETCH="skip", AVA_PROCESS_PROFILE="gateway")
    return subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [sys.executable, "-X", "utf8", str(_PROBE), *args],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_scheduler_daemon_import_does_not_load_the_oss_sdk(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    proc = _fresh(["daemon", str(_REPO_ROOT), *_SDK_ROOTS], home)

    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    chain = " <- ".join(reversed(report["first_importer"]))
    assert report["loaded"] == [], (
        f"the pg-backup scheduler daemon imports {_SDK_ROOTS} at boot but only its worker "
        f"subprocess publishes off-site. First repo frames that reached the SDK: {chain}. "
        "Make that import lazy, inside the function that uploads"
    )


def test_backup_worker_publish_leg_still_loads_and_uses_the_oss_sdk(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    proc = _fresh(
        [
            "worker",
            str(_REPO_ROOT),
            str(tmp_path / "oss.json"),
            str(tmp_path / "work"),
            *_SDK_ROOTS,
        ],
        home,
    )

    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["before"] == [], "importing the backup modules must not load the SDK"
    assert report["bucket_class"] == "Bucket"
    assert set(report["result"]) == {"artifact", "sha256"}
    # The publish ran for real: the store opened (an unreadable credentials file would
    # say "store unavailable") and the upload was refused for the empty artifact.
    assert "[backup] off-site store unavailable" not in proc.stderr
    assert "off-site upload requires a non-empty artifact" in proc.stderr
