"""The boot-autostart retry policy — one behaviour, stated once.

At boot, the things `ava start` needs are not all up yet. The incident that
produced this module: an enrolled agent-runner fetches `GET /api/bootstrap`
from the gateway as part of its Settings build (`base.host.env.bootstrap`; every
runner process does this at startup), the VPN interface came up *after* the
boot job fired, the fetch raised ENETUNREACH, `ava start` exited 1 — and
nothing ever tried again, so the runner stayed down for hours. Any transient
boot-time dependency (DNS, a not-yet-mounted volume, a gateway still starting)
has that same shape.

The policy: **re-run `ava start` every ``BOOT_RETRY_INTERVAL_S`` seconds until
it exits 0, at most ``BOOT_MAX_ATTEMPTS`` times per boot.** Identical on both
supported platforms; the scheduler supplies the retry and the boot job itself
(`ava start --boot`) supplies the cap, so the stop and its alert are the same
on both:

- **macOS** — launchd does the retrying (`KeepAlive` → `SuccessfulExit: false`,
  `ThrottleInterval` = the interval). launchd has no attempt limit, so the
  failing attempt that spends the budget exits 0: that is the only exit launchd
  does not retry. Preferred where it exists: launchd is a supervisor, so it also
  restarts a start that *died* rather than exited, which a loop living inside
  that process could not.
- **Linux** — converge registers the sole automatic boot route, the systemd
  unit (`base.host.system.boot_unit`). `Restart=on-failure`, `RestartSec` = the
  interval, and `StartLimitBurst` = the budget inside `StartLimitIntervalSec`
  state the policy natively; the unit then sits in systemd's `failed` state.
  `TimeoutStartSec` bounds initial readiness. Type=forking adopts the
  birth-validated root PID after ordinary start exits successfully; no resident
  wrapper or root runtime cap exists. Automatic boot requires systemd.
  Interactive start may launch root directly.

**Why an attempt cap.** A permanent error (a broken `.env`, a missing
dependency, an unreachable gateway that never comes back) must not be hidden
behind an endless restart loop, and ava-root already gives each service a
finite restart budget; the boot job is the same kind of supervisor and gets the
same kind of bound. Ten attempts cover about ten minutes of ordinary boot-time
dependency lag. The failing attempt that spends the budget emits
`boot_retries_exhausted` (alert `ava-ops-boot-retries-exhausted`) and leaves the
cause in the boot log; a human then runs `ava start`. The first successful start
ends the boot attempt; later service health belongs to ava-root. Repeating start
reconciles persisted identity and owned services; it must not replace a healthy
generation just because another unit is unready.

**Readiness is required.** Every boot mechanism runs the ordinary start entry.
Unready critical services keep a nonzero result and remain visible in the boot
log. Root owns service revival, so retries do not create another service owner
or claim readiness before the application's probes pass. A boot attempt does not
override ava-root's own restart budget: it does not re-up a unit whose budget is
spent.

**The count.** One small JSON file per home records the boot it belongs to and
the attempts made so far. A different boot (or a missing/unreadable file) starts
the count again; a successful start deletes it. The count only decides whether a
*failed* attempt asks its scheduler to retry; it never refuses to run `ava start`,
so a human restarting the boot job after fixing the cause always gets a real
attempt.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import psutil
from loguru import logger

from base.host.atomic_io import write_text_atomic
from base.native_process.os_platform import is_macos

# Seconds between two `ava start` attempts — launchd's ThrottleInterval on
# macOS and systemd RestartSec on Linux.
BOOT_RETRY_INTERVAL_S = 60

# Boot attempts per boot: systemd's StartLimitBurst on Linux, the cap inside
# `ava start --boot` on both platforms.
BOOT_MAX_ATTEMPTS = 10

_ATTEMPTS_FILE: Final = "boot-attempts.json"
# `psutil.boot_time()` is derived from the wall clock on some kernels and can
# drift by a second or so between reads; a real reboot moves it by far more.
_SAME_BOOT_TOLERANCE_S: Final = 60.0


def _attempts_path(home: Path) -> Path:
    return home / "run" / _ATTEMPTS_FILE


def _recorded_attempts(home: Path, boot_time: float) -> int:
    """Attempts already made in the boot at ``boot_time`` (0 for any other boot)."""
    path = _attempts_path(home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        recorded_boot = float(raw["boot_time"])
        attempts = int(raw["attempts"])
    except FileNotFoundError:
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # The writer is atomic, so an unreadable file was altered from outside.
        # Failing here would fail every boot attempt for ever; counting again
        # from one is bounded, because this attempt rewrites the file.
        logger.warning("boot attempt record {} is unreadable ({}); counting again", path, exc)
        return 0
    if abs(recorded_boot - boot_time) > _SAME_BOOT_TOLERANCE_S or attempts < 0:
        return 0
    return attempts


def begin_attempt(home: Path) -> int:
    """Record one boot-job `ava start` and return its 1-based number within this boot."""
    boot_time = psutil.boot_time()
    attempt = _recorded_attempts(home, boot_time) + 1
    path = _attempts_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        path,
        json.dumps({"boot_time": boot_time, "attempts": attempt}),
        mode=0o600,
        sync_parent=True,
    )
    return attempt


def clear_attempts(home: Path) -> None:
    """A successful start ends the boot attempt."""
    _attempts_path(home).unlink(missing_ok=True)


def budget_spent(attempt: int) -> bool:
    """Whether a failed attempt number ``attempt`` is the last the policy allows."""
    return attempt >= BOOT_MAX_ATTEMPTS


def exit_code_after_budget(failed_code: int) -> int:
    """The exit code of the failing attempt that spends the budget.

    launchd retries every nonzero exit and has no attempt limit, so on macOS the
    last attempt reports 0 to stop it (the failure itself is the event and the
    boot log). systemd keeps the real code: its own start limit stops the unit
    in the `failed` state.
    """
    return 0 if is_macos() else failed_code
