"""`ava boot` — `ava start`, re-run while the machine is still coming up.

The boot job's entry on the platforms whose scheduler cannot retry a failed job
for us (Linux `@reboot`, Windows `ONLOGON`); macOS lets launchd do it instead.
Which, and why, is `shared/boot_policy.py`.

Retries with no attempt limit, which is what launchd does on the platform that
has a scheduler-level answer -- the three platforms must agree, or a box whose
VPN is down for 45 minutes recovers on one and stays down forever on the others.

Not an argparse subcommand: `cli.main` dispatches it by argv before the
settings-gated `cli.commands` import — the same early slot as `ava enroll` —
because the boot job must be able to retry a start that failed for *any* reason,
a settings error included.

`ava start` runs as a child process rather than in-process. A child turns a hard
crash — signal, interpreter death — into an ordinary non-zero return code, so
the retry covers those too. (It also keeps this loop's own process image intact:
an enrolled runner's start may fail at Settings build when the gateway fetch
fails, but it never re-execs itself anymore — the 2026-08-01 config refactor
deleted the `.env` refresh + re-exec in favor of fetching at every start.)
"""

from __future__ import annotations

import subprocess
import sys
import time

from shared.boot_policy import BOOT_RETRY_INTERVAL_S


def _start_command(start_args: list[str]) -> list[str]:
    """The child command: this interpreter running `ava start`.

    `sys.executable -m cli.main` rather than the `ava` console script. The
    scheduled job already names a specific interpreter (the Windows task names
    the venv's `pythonw.exe` outright, precisely so PATH lookup cannot pick a
    different `ava.exe`); re-deriving the binary here would reopen that.

    `--no-readiness-gate` is appended, not forwarded: the loop below retries any
    non-zero code forever, so it must only ever see codes a retry can fix. A start
    that could not launch its services still exits 1 and is still retried -- that is
    the ENETUNREACH class this module exists for. A start that launched them and has
    one not yet serving exits 0 here on purpose, because the alternative is a box
    where a headed Chrome that will never launch means `ava start` runs every 60
    seconds forever. Reviving a launched-but-down service belongs to the watchdog
    keepalive, which this start has just brought up. The macOS plist
    (`shared.os_autostart`) passes the same flag for the same reason: launchd cannot
    read exit codes past zero/non-zero, so the three platforms can only agree if the
    boot path never produces a readiness code at all.
    """
    return [sys.executable, "-m", "cli.main", "start", *start_args, "--no-readiness-gate"]


def run_boot(argv: list[str]) -> int:
    """Run `ava start` until it exits 0. Returns 0; does not give up before that.

    No attempt cap, matching what launchd does on macOS. Nothing else recovers a
    host whose boot start never succeeded -- the OS watchdog probe revives a dead
    watchdog and nothing more -- so giving up would leave the box down until a
    human noticed, which is the outage this exists to prevent. See
    `shared/boot_policy.py`.

    `argv` is forwarded verbatim to `ava start`, so the boot job can carry
    whatever flags a hand-run start would.
    """
    command = _start_command(argv)
    attempt = 0
    while True:
        attempt += 1
        rc = subprocess.run(command, check=False).returncode  # noqa: S603 — fixed argv, no shell
        if rc == 0:
            return 0
        print(
            f"[ava boot] `ava start` failed (rc={rc}) on attempt {attempt}; "
            f"retrying in {BOOT_RETRY_INTERVAL_S}s",
            file=sys.stderr,
        )
        time.sleep(BOOT_RETRY_INTERVAL_S)
