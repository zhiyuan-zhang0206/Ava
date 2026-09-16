"""Consecutive-boot deferral streaks for hosted-force recovery (task #3619).

Hosted boot recovery may defer for an agent whose retained exec request
evidence is not yet disposable. One warning per boot rots silently, so every
boot rewrites a host-local ledger (``$AVA_HOME/run`` — no schema migration):
an agent deferred again continues its streak, and every other agent's streak
ends by absence (recovered, settled, or no longer a candidate). The daemon
promotes its per-boot warning to the ``hosted_boot_recovery_stalled`` anomaly
event once a streak reaches ``ALERT_AFTER_BOOTS``.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Collection
from pathlib import Path
from typing import cast

from shared import paths
from shared.log import logger

# Consecutive boots a hosted-force recovery may stay deferred before the
# per-boot warning escalates to the hosted_boot_recovery_stalled anomaly event.
# One boot can be a race (the retained evidence settles right after) and two
# can be an unlucky restart cadence; a third consecutive boot with the same
# agent deferred is a stall that would otherwise rot silently.
ALERT_AFTER_BOOTS = 3
_STATE_FILENAME = "hosted-boot-recovery-defers.json"


def state_path() -> Path:
    """The host-local consecutive-boot deferral ledger ($AVA_HOME/run)."""
    return paths.run_dir() / _STATE_FILENAME


def read_streaks() -> dict[int, int]:
    """Read the ledger; a damaged file resets the streaks it cannot carry.

    The ledger is bookkeeping for an alert, never authority: a torn or
    unknown-schema file must not fail a boot, so it reads as empty and the
    streaks start over.
    """
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("hosted boot defer ledger unreadable; streaks reset: {error}", error=exc)
        return {}
    if not isinstance(raw, dict):
        logger.warning("hosted boot defer ledger is not an object; streaks reset")
        return {}
    streaks: dict[int, int] = {}
    for key, value in cast("dict[object, object]", raw).items():
        if isinstance(key, str) and key.isdigit() and isinstance(value, int) and value > 0:
            streaks[int(key)] = value
    return streaks


def write_streaks(streaks: dict[int, int]) -> None:
    """Persist the ledger atomically; a failure is loud but never fails the boot."""
    path = state_path()
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_tmp = tempfile.mkstemp(dir=path.parent, prefix=".boot-defers.", suffix=".tmp")
        tmp = Path(raw_tmp)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({str(key): value for key, value in sorted(streaks.items())}, stream)
        tmp.replace(path)
    except OSError as exc:
        logger.warning("hosted boot defer ledger write failed: {error}", error=exc)
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)


def record_deferrals(deferred_agents: Collection[int]) -> dict[int, int]:
    """Count each deferred agent's consecutive boots and drop every other streak.

    One rewrite per boot: an agent deferred in this boot continues its streak
    (its first deferral = 1); every agent that recovered, settled, or vanished
    from the scan ends its streak by being absent from the new ledger.
    """
    previous = read_streaks()
    streaks = {agent_id: previous.get(agent_id, 0) + 1 for agent_id in sorted(deferred_agents)}
    write_streaks(streaks)
    return streaks
