"""CI-only native read evidence: never installs or changes an OS job."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta

from shared.native_job_observation import (
    NativeReadUnavailableError,
    launchd_loaded,
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
            labels = read_launchd_labels(until)
            gui_enumeration = True
        except NativeReadUnavailableError:
            gui_enumeration = False
            labels = frozenset()
        exact_gui_lookup = False
        for label in sorted(labels):
            if label.startswith("com.apple.") and launchd_loaded(label, until) is True:
                exact_gui_lookup = True
                break
        if gui_enumeration and labels and not exact_gui_lookup:
            raise RuntimeError("enumerated GUI services had no positive exact lookup")
        print(
            json.dumps(
                {
                    "platform": "darwin",
                    "realLaunchctlRead": True,
                    "effectiveEnabledProof": False,
                    "guiEnumerationAvailable": gui_enumeration,
                    "exactGuiLookup": exact_gui_lookup,
                    "writes": False,
                    "closure": "unknown",
                }
            )
        )
    else:
        raise RuntimeError("native launcher proof platform unsupported")


if __name__ == "__main__":
    main()
