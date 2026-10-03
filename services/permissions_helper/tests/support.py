"""Test support: a stopped helper job definition on disk."""

from __future__ import annotations

from pathlib import Path


def stopped_helper_plist(home: Path, path: Path) -> bytes:
    import plistlib

    from services.permissions_helper import launchd_job as jobs

    body = plistlib.dumps(
        {
            "Label": jobs.helper_job_label(home),
            "ProgramArguments": ["/private/test/AvaPermissionsHelper"],
            "KeepAlive": {"SuccessfulExit": False},
            "EnvironmentVariables": {
                "AVA_PERMISSIONS_HELPER_SOCKET": str(home / "run/permissions-helper.23456.sock"),
                "AVA_PERMISSIONS_HELPER_ROOT_SEED": str(home / "run/ava-root/seed.json"),
            },
        }
    )
    path.write_bytes(body)
    return body
