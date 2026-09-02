"""CI-only native read evidence: never installs or changes an OS job."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta

from shared.native_job_observation import (
    NativeReadUnavailableError,
    native_read,
    read_crontab,
    read_launchd_labels,
)


def main() -> None:
    until = datetime.now(UTC) + timedelta(seconds=20)
    if sys.platform == "linux":
        before = read_crontab(until)
        after = read_crontab(until)
        if before != after:
            raise RuntimeError("native crontab drifted during read-only proof")
        print(
            json.dumps(
                {
                    "platform": "linux",
                    "realCrontabRead": True,
                    "writes": False,
                    "closure": "unknown",
                }
            )
        )
    elif sys.platform == "darwin":
        # Real OS command, not a fake launchctl. Do not parse diagnostic output
        # or manufacture an Ava job just to obtain a positive test fixture.
        result = native_read(("/bin/launchctl", "print", "system"), until)
        if result.returncode != 0 or not result.stdout:
            raise RuntimeError("native launchctl system domain is unreadable")
        try:
            read_launchd_labels(until)
            gui_enumeration = True
        except NativeReadUnavailableError:
            gui_enumeration = False
        print(
            json.dumps(
                {
                    "platform": "darwin",
                    "realLaunchctlRead": True,
                    "effectiveEnabledProof": False,
                    "guiEnumerationAvailable": gui_enumeration,
                    "writes": False,
                    "closure": "unknown",
                }
            )
        )
    else:
        raise RuntimeError("native launcher proof platform unsupported")


if __name__ == "__main__":
    main()
