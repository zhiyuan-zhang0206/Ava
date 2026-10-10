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

# Records, at the first import of the SDK, the repo frames that asked for it.
_DAEMON_PROBE = """
import importlib.abc
import json
import sys
import traceback

sys.path.insert(0, {repo!r})
first_importer: list[str] = []


class _Spy(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {roots!r} and not first_importer:
            first_importer.extend(
                f"{{frame.filename[len({repo!r}) + 1:]}}:{{frame.lineno}}"
                for frame in traceback.extract_stack()
                if frame.filename.startswith({repo!r}) and "/.venv/" not in frame.filename
            )
        return None


sys.meta_path.insert(0, _Spy())
import services.backup.scheduler.daemon

print(json.dumps({{
    "loaded": sorted(m for m in sys.modules if m.split(".")[0] in {roots!r}),
    "first_importer": first_importer,
}}))
"""

# The worker's dump leg with the pg_dump pipeline replaced by an empty artifact:
# the real `run_backup` reaches its real off-site publish, which refuses an empty
# artifact after opening the bucket with the SDK and before any network I/O.
_WORKER_PROBE = """
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, {repo!r})
logging.basicConfig(stream=sys.stderr, level=logging.INFO)

import services.backup.dump as backup
from base.config import ConfigBoot
config = ConfigBoot()
settings = config.view
from services.backup.scheduler import worker

before = sorted(m for m in sys.modules if m.split(".")[0] in {roots!r})
credentials = Path({credentials!r})
credentials.write_text(json.dumps({{"access_key_id": "id", "access_key_secret": "secret"}}))
settings.services.backup_offsite_endpoint = "https://oss.invalid"
settings.services.backup_offsite_bucket = "backups"
settings.services.backup_offsite_credentials_file = credentials


def _empty_dump(now, *, directory, **_):
    artifact = directory / "ava-20261002T030000Z.dump.enc"
    artifact.write_bytes(b"")
    return artifact


backup._run_backup = _empty_dump
work = Path({work!r})
work.mkdir()
result = worker._execute(
    {{"kind": "dump", "now": datetime(2026, 10, 2, 3, tzinfo=UTC).isoformat()}}, work, config=config
)
print(json.dumps({{
    "before": before,
    "result": result,
    "bucket_class": sys.modules["oss2"].Bucket.__name__,
}}))
"""


def _fresh(code: str, home: Path) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env.update(AVA_HOME=str(home), AVA_CONFIG_FETCH="skip", AVA_PROCESS_PROFILE="gateway")
    return subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [sys.executable, "-X", "utf8", "-c", code],
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

    proc = _fresh(_DAEMON_PROBE.format(repo=str(_REPO_ROOT), roots=_SDK_ROOTS), home)

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
        _WORKER_PROBE.format(
            repo=str(_REPO_ROOT),
            roots=_SDK_ROOTS,
            credentials=str(tmp_path / "oss.json"),
            work=str(tmp_path / "work"),
        ),
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
