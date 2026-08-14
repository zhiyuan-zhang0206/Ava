"""Persistent heal record — the backoff a self-healing controller keeps across the
restart its own heal causes.

A controller that converges by restarting this host cannot hold its backoff in
process memory: the heal it spawns is what kills the process holding it. So each
acting controller writes one small JSON file under `$AVA_HOME` and reads it back
in the next process. The process-level cooldown (`update_trigger.in_cooldown`)
covers consecutive ticks of ONE process; this covers the spawn → restart → spawn
cycle, which is the one that can become a restart loop.

Record shape (one file per dimension, path supplied by the caller):
`{'target': <sha>, 'ts': <epoch>, 'ok': <bool>, 'consecutive_failures': <int>,
'last_error': <str|null>}`. Failures are recorded as well as successes, so
`ava cluster status` and the health probe can answer "how long has this host been
stuck" — a host that cannot converge otherwise gets quieter the more stuck it is.

Backward compatibility: records written before this module was extracted key the
target commit as `pin` and may use the older plain-text `<sha>\\n<ts>` form; both
are read as `target`.
"""

from __future__ import annotations

import json
import time
from contextlib import suppress
from pathlib import Path


def read_record(path: Path) -> dict[str, object] | None:
    """The heal record at `path` as a dict, or None if absent / unparseable.

    Normalizes the legacy `pin` key and the legacy plain-text `<sha>\\n<ts>` form
    onto the current `target` key.
    """
    try:
        raw = path.read_text().strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if raw.startswith("{"):
        try:
            rec: dict[str, object] = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if "target" not in rec and "pin" in rec:
            rec["target"] = rec["pin"]
        return rec
    parts = raw.split("\n")
    if len(parts) >= 2:
        try:
            return {
                "target": parts[0],
                "ts": float(parts[1]),
                "ok": True,
                "consecutive_failures": 0,
                "last_error": None,
            }
        except ValueError:
            return None
    return None


def in_backoff(path: Path, target: str, window_s: float) -> bool:
    """True if a heal toward *this* target was attempted within `window_s`.

    By the caller's contract the host is still off the target when this is asked,
    so a recent attempt means the previous one did not land — retrying now would
    just thrash. A different target, or an expired / absent / unreadable record,
    is not in backoff.
    """
    rec = read_record(path)
    if rec is None:
        return False
    ts = rec.get("ts", 0)
    if not isinstance(ts, (int, float)):
        return False
    return rec.get("target") == target and (time.time() - ts) < window_s


def record_attempt(path: Path, target: str, *, ok: bool = True, error: str | None = None) -> None:
    """Record one heal attempt toward `target` — successes AND failures.

    `consecutive_failures` carries over only across failures toward the same
    target; any success, or a new target, restarts the count.

    The carry-over is read inside the `not ok` branch, which is what makes the
    "any success restarts the count" half true. Read outside it, a success that
    followed a failure kept the previous count and wrote `ok=True` beside
    `consecutive_failures=1` — a record that says the host both healed and is one
    round into being stuck. Nothing in the product branched on the field (it is
    read by operator surfaces), so it misreported rather than misbehaved, and
    `update_trigger`'s in-process sibling counter has always reset on success.
    """
    consecutive = 0
    if not ok:
        prev = read_record(path)
        if prev is not None and prev.get("target") == target and not prev.get("ok"):
            prev_failures = prev.get("consecutive_failures", 0)
            consecutive = prev_failures if isinstance(prev_failures, int) else 0
        consecutive += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "target": target,
                "ts": time.time(),
                "ok": ok,
                "consecutive_failures": consecutive,
                "last_error": error,
            }
        )
    )


def clear(path: Path) -> None:
    """Drop the record — the dimension converged, so the backoff must not outlive it."""
    with suppress(OSError):
        path.unlink(missing_ok=True)
