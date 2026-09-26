"""The disposable permissions-helper app shared by the opt-in native macOS tests.

This checkout's helper is compiled and signed ad hoc with an explicit designated
requirement, or with the production stable identity when
AVA_NATIVE_SIGNED_HELPER=1 (``build_and_sign`` into a disposable directory).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from services.permissions_helper import hardened_runtime, lifecycle

BUNDLE_REQUIREMENT = f'identifier "{lifecycle.HELPER_BUNDLE_ID}"'


def run(argv: list[str], *, timeout: float = 60) -> str:
    result = subprocess.run(  # noqa: S603 — fixed native tools and disposable fixture paths
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode:
        raise RuntimeError(f"native fixture command failed: {argv!r}: {result.stderr}")
    return result.stdout


def stable_identity() -> bool:
    return os.environ.get("AVA_NATIVE_SIGNED_HELPER") == "1"


def build_helper_app(root: Path, *, hardened: bool = True) -> Path:
    """A signed AvaPermissionsHelper.app of this checkout under ``root``.

    ``hardened=False`` reproduces the pre-fix ad-hoc signature without the
    hardened runtime, only as the injection control of the regression test.
    """
    if stable_identity() and hardened:
        app, _rebuilt = lifecycle.build_and_sign(destination=root)
        return app
    app = root / "AvaPermissionsHelper.app"
    executable = app / "Contents/MacOS/AvaPermissionsHelper"
    executable.parent.mkdir(parents=True)
    shutil.copyfile(lifecycle._INFO_PLIST, app / "Contents/Info.plist")
    run(["swiftc", "-O", str(lifecycle._SOURCE), "-o", str(executable)], timeout=300)
    run(
        [
            "codesign",
            "--force",
            "--sign",
            "-",
            *(hardened_runtime.SIGNING_OPTIONS if hardened else ()),
            "--identifier",
            lifecycle.HELPER_BUNDLE_ID,
            "--requirements",
            f"=designated => {BUNDLE_REQUIREMENT}",
            str(app),
        ]
    )
    return app
