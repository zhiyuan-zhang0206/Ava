"""Fresh-interpreter probes for the scheduler daemon's OSS SDK import boundary.

Usage: `child_import_probe.py daemon <repo> <roots...>` records, at the first
import of the SDK, which repo frames asked for it while the daemon is imported.
`child_import_probe.py worker <repo> <credentials> <work> <roots...>` runs the
worker's dump leg with an empty artifact so the real off-site publish opens the
bucket with the SDK. Both print one JSON report line on stdout.
"""

import importlib.abc
import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path


def _daemon_probe(repo: str, roots: tuple[str, ...]) -> None:
    first_importer: list[str] = []

    class _Spy(importlib.abc.MetaPathFinder):
        def find_spec(self, name: str, path: object = None, target: object = None) -> None:
            if name.split(".", maxsplit=1)[0] in roots and not first_importer:
                first_importer.extend(
                    f"{frame.filename[len(repo) + 1 :]}:{frame.lineno}"
                    for frame in traceback.extract_stack()
                    if frame.filename.startswith(repo) and "/.venv/" not in frame.filename
                )

    sys.meta_path.insert(0, _Spy())
    import services.backup.scheduler.daemon  # noqa: F401  # pyright: ignore[reportUnusedImport]

    sys.stdout.write(
        json.dumps(
            {
                "loaded": sorted(m for m in sys.modules if m.split(".")[0] in roots),
                "first_importer": first_importer,
            }
        )
        + "\n"
    )


def _worker_probe(credentials_path: str, work_path: str, roots: tuple[str, ...]) -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)

    import services.backup.dump as backup
    from base.config import ConfigBoot
    from base.db import Database
    from base.db.code_version_gate import ProcessDbGate
    from base.native_process.code_version import CodeVersion
    from base.native_process.loaded_commit import LoadedCommit

    version = CodeVersion(LoadedCommit.capture())
    gate = ProcessDbGate(version=version.get, process="backup-test")
    config = ConfigBoot()
    settings = config.view
    from services.backup.scheduler import worker

    before = sorted(m for m in sys.modules if m.split(".")[0] in roots)
    credentials = Path(credentials_path)
    credentials.write_text(json.dumps({"access_key_id": "id", "access_key_secret": "secret"}))
    settings.services.backup_offsite_endpoint = "https://oss.invalid"
    settings.services.backup_offsite_bucket = "backups"
    settings.services.backup_offsite_credentials_file = credentials

    def _empty_dump(_now: object, *, directory: Path, **_: object) -> Path:
        artifact = directory / "ava-20261002T030000Z.dump.enc"
        artifact.write_bytes(b"")
        return artifact

    backup.run_locked_backup = _empty_dump
    work = Path(work_path)
    work.mkdir()

    def no_restore(url: str) -> Database:
        raise AssertionError("dump must not restore")

    result = worker.execute(
        {"kind": "dump", "now": datetime(2026, 10, 2, 3, tzinfo=UTC).isoformat()},
        work,
        config=config,
        database=lambda: Database.from_settings(gate=gate),
        database_for_url=no_restore,
    )
    sys.stdout.write(
        json.dumps(
            {
                "before": before,
                "result": result,
                "bucket_class": sys.modules["oss2"].Bucket.__name__,
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    if sys.argv[1] == "daemon":
        _daemon_probe(sys.argv[2], tuple(sys.argv[3:]))
    elif sys.argv[1] == "worker":
        _worker_probe(sys.argv[3], sys.argv[4], tuple(sys.argv[5:]))
    else:
        raise SystemExit(f"unknown probe {sys.argv[1]!r}")
