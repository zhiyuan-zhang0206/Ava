"""A disposable readiness caller for the real generic root, not a deployed wrapper.

Run by the native-systemd test in place of the boot unit's `ExecStart`:
`systemd_starter.py <parameter file>`, a JSON file that names `receipt`, `home`, `data_command`,
`root_command`, `socket` and `failure` ("", "before" or "after" publication). It starts a
data-plane sibling and the root, waits until root runs its unit, records each birth in the
receipt, publishes readiness, and fails on purpose where `failure` says.
"""

import contextlib
import json
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import psutil

from base.host.system.boot_unit import publish_root_ready, root_pid_path
from base.native_process.ownership import OwnedProcess
from base.native_process.root_control.client import RootClient, RootClientError, native_identity


def main(params: dict[str, Any]) -> None:
    receipt = Path(params["receipt"])
    home = Path(params["home"])
    failure = params["failure"]
    data = subprocess.Popen(params["data_command"], start_new_session=True)  # noqa: S603 - the test's own command
    hint = root_pid_path(home)
    hint.parent.mkdir(parents=True, exist_ok=True)
    hint.write_text(str(data.pid) + "\n")
    births = {"data": asdict(OwnedProcess.capture(psutil.Process(data.pid)))}
    receipt.write_text(json.dumps(births))
    root = subprocess.Popen(params["root_command"], start_new_session=True)  # noqa: S603 - the test's own command
    births["root"] = asdict(OwnedProcess.capture(psutil.Process(root.pid)))
    receipt.write_text(json.dumps(births))
    client = RootClient(Path(params["socket"]), timeout=1)
    deadline = time.monotonic() + 20
    while True:
        with contextlib.suppress(RootClientError, KeyError):
            status: dict[str, Any] = cast("dict[str, Any]", client.status())["result"]
            if status["units"][0]["state"] == "running":
                break
        if root.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError("root unready")
        time.sleep(0.05)
    births["app"] = asdict(native_identity(status["units"][0]))
    receipt.write_text(json.dumps(births))
    if failure == "before":
        receipt.with_suffix(".failure").write_text("before")
        raise RuntimeError("failed before publication")
    publish_root_ready(home, native_identity(status["root"]))
    if failure == "after":
        receipt.with_suffix(".failure").write_text("after")
        raise RuntimeError("failed after publication")


if __name__ == "__main__":
    main(json.loads(Path(sys.argv[1]).read_text()))
