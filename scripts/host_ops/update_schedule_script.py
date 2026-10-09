#!/usr/bin/env python
"""Version a script during certified maintenance using the installed runtime.

This file may be copied from a reviewed revision and run outside the installed
checkout. All application imports come from --source, never from this file's
checkout. The existing installed schedule writer owns the transaction, version
snapshot and deferred convergence request; this adapter never launches a session.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path
from typing import Any


def script_sha256(script: str) -> str:
    """Digest the UTF-8 stored text without newline conversion."""
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def require_stopped_hold() -> Any:
    """Require a healthy certified stop; a retained live root is allowed."""
    from base.deploy.maintenance import admission
    from base.deploy.maintenance.state import MaintenancePhase

    current = admission.snapshot()
    if (
        current is None
        or current.maintenance is None
        or current.maintenance.phase != MaintenancePhase.STOPPED
        or current.maintenance.failures
    ):
        raise RuntimeError("script repair requires a failure-free stopped maintenance hold")
    return current


def load_installed_runtime(home: Path, source: Path) -> None:
    """Select the explicit installed source and let its owner check admission."""
    if Path(sys.prefix).resolve() != (source / ".venv").resolve():
        raise RuntimeError("run this tool with the source checkout's own .venv interpreter")
    # A copied operator script must not import its containing development tree.
    sys.path.insert(0, str(source))
    from base.host.env.dotenv_boot import launcher_context, resolve_ava_home, skip_config_fetch

    if resolve_ava_home().expanduser().resolve() != home:
        raise RuntimeError("--home must match this operator's AVA_HOME")
    if launcher_context() is not None:
        raise RuntimeError("run as a local operator without a launcher credential delivery")
    skip_config_fetch()
    from base.cluster.authority.delivery import require_admitted_runtime
    from base.paths import repo_root

    if repo_root().resolve() != source:
        raise RuntimeError("loaded application modules do not belong to the requested source")
    require_admitted_runtime(home, code_root=source)


def replace_script(pool: Any, schedule_id: int, script: str, expected_sha256: str) -> str:
    """Check the old text and reuse the installed versioned writer.

    The caller holds the home's lifecycle mutex and stopped journal. Business
    API writes are fenced and the schedule manager is quiesced throughout this
    precondition/read/write interval. Per-row commits are deliberate: after an
    interruption, the prepared text is a no-op and any third text is refused.
    """
    # First-generation bootstrap must use the already installed versioned writer.
    # Calling a newly introduced public owner would require deploying it first.
    # This narrow coupling is covered on 44d and current source; no SQL is copied.
    from gateway.schedules.router import _fetch_full_blocking, _update_blocking

    previous = _fetch_full_blocking(pool, schedule_id)
    actual = script_sha256(previous[9])
    prepared = script_sha256(script)
    if actual == prepared:
        return "unchanged"
    if actual != expected_sha256:
        raise RuntimeError(
            f"schedule {schedule_id}: expected old script {expected_sha256}, found {actual}"
        )
    row, _needs_sync = _update_blocking(pool, schedule_id, {"script": script})
    # _FULL_COLS: identity/config/status/history facts precede updated_at/script.
    if row[:8] != previous[:8] or row[9] != script:
        raise RuntimeError(f"schedule {schedule_id}: installed writer changed protected fields")
    return "updated"


def repair_script(
    *, home: Path, source: Path, schedule_id: int, script: str, expected_sha256: str
) -> str:
    """Run one script-only edit without releasing or progressing maintenance."""
    load_installed_runtime(home, source)
    from base.db import Database
    from base.deploy.lifecycle.home_lifecycle_locks import resource_lock

    with resource_lock(purpose="operator.update_schedule_script"):
        before = require_stopped_hold()
        with Database.from_settings().pool() as pool:
            result = replace_script(pool, schedule_id, script, expected_sha256)
        if require_stopped_hold() != before:
            raise RuntimeError("maintenance generation changed during script repair")
    return result


def old_script_digest(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError("expected a lowercase SHA-256 hex digest")
    return value


def positive_id(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("schedule id must be positive")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schedule_id", type=positive_id)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--script-file", required=True)
    parser.add_argument("--expected-sha256", required=True, type=old_script_digest)
    args = parser.parse_args(argv)
    try:
        raw = (
            sys.stdin.buffer.read()
            if args.script_file == "-"
            else Path(args.script_file).read_bytes()
        )
        script = raw.decode("utf-8")
        compile(script, "<schedule>", "exec")
        result = repair_script(
            home=args.home.expanduser().resolve(),
            source=args.source.expanduser().resolve(),
            schedule_id=args.schedule_id,
            script=script,
            expected_sha256=args.expected_sha256,
        )
    except Exception as exc:
        print(f"script repair failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"schedule {args.schedule_id}: {result} script_sha256={script_sha256(script)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
