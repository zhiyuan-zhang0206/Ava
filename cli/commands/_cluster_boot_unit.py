"""Linux boot-unit CLI: ordinary start hands systemd the verified app root."""

from __future__ import annotations

import sys


def cmd_boot_unit_install(*, enable: bool = True, start: bool = False) -> int:
    """Install the boot unit; enable by default.

    `--no-enable` is the staged form (drills, reviewed rollouts): the files
    land, the crontab entry stays the live boot path until a later enable.
    """
    from shared.os_boot_unit import install

    try:
        steps = install(enable=enable, start=start)
    except (RuntimeError, ValueError) as e:
        print(f"  * boot-unit install failed: {e}", file=sys.stderr)
        return 1
    for step in steps:
        print(f"  + {step}")
    if enable:
        print("  . the unit owns the boot path now; no crontab entry remains")
    else:
        print("  . staged install: the crontab entry stays live until enabled")
    return 0


def cmd_boot_unit_uninstall() -> int:
    """Remove this home's boot unit after a successful native stop.

    The crontab path re-arms on the next `ava start` (converge registers it
    once the unit is gone) -- that is the rollback.
    """
    from shared.os_boot_unit import uninstall

    try:
        steps = uninstall()
    except RuntimeError as e:
        print(f"  * boot-unit uninstall failed: {e}", file=sys.stderr)
        return 1
    for step in steps:
        print(f"  + {step}")
    if not steps:
        print("  . nothing installed")
    print("  . the crontab path re-arms on the next `ava start`")
    return 0


def cmd_boot_unit_status() -> int:
    """Print the boot unit's read-only operator report (see `shared.os_boot_unit`)."""
    from shared.os_boot_unit import status

    try:
        rows = status()
    except (RuntimeError, ValueError) as e:
        print(f"  * boot-unit status failed: {e}", file=sys.stderr)
        return 1
    for label, value in rows:
        print(f"  {label + ':':<18} {value}")
    return 0
