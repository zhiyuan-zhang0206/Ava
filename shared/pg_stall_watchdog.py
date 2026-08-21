"""Stall watchdog for throwaway Postgres clusters (issue #145).

A throwaway cluster that stops answering is invisible until the suite times
out: the symptom on 2026-08-20 was two concurrent suites wedged mid-run (~40
min elapsed, ~2 min CPU) with a bare `psql` to the stuck instance hanging
too, and no captured evidence of why. This module watches a cluster for its
usable lifetime and converts a stall into a fast, diagnosed failure: after
the cluster stops answering `SELECT 1` for a few probe intervals, dump what
the host still knows (process states, the postmaster's stack on macOS, any
rows the admin connection answers) into the fixture-log artifact dir, then
SIGKILL the postmaster so the waiting suite fails immediately with a
connection error instead of hanging for the rest of the run.

Owned by `shared/pg_tools.py`'s throwaway-cluster lifecycle; extracted here
to keep that module under its line ceiling.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from shared.log import logger
from shared.platform import IS_MACOS

# How long the cluster may stay unresponsive before we call it stalled and
# fail fast. Probe cadence is deliberately coarse: the cluster is disposable
# test infrastructure, and a real stall costs the suite minutes either way.
_PROBE_INTERVAL_S = 5.0
_PROBE_TIMEOUT_S = 4
_PROBE_CONSECUTIVE_FAILURES = 2


def fixture_log_artifact_dir() -> Path:
    """Where failed-cluster artifacts (pg.log, stall evidence) are preserved.

    Derived here rather than exposed as a Settings knob: nothing outside the
    test fixtures reads it, and CI names the same fixed path in its
    upload-artifact step. Mirrors the e2e-logs convention
    (`tests/e2e/conftest.py::_LOG_DIR`). Issue #1037: without this, the reason
    `pg_ctl` refused to start lives in the tmpfs and is deleted with the
    instance dir."""
    from shared.paths import repo_root

    return repo_root() / "tmp" / "pg-fixture-logs"


def postmaster_pids(port: int) -> list[tuple[int, str]]:
    """(pid, ps-state-line) for postmasters whose command line names `port`
    (`pg_ctl -o` args survive verbatim in the postmaster's argv, so `-p
    <port>` is present). Best-effort — a process list that cannot be read
    yields nothing rather than raising."""
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,state=,etime=,command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    found: list[tuple[int, str]] = []
    for line in out.splitlines():
        if f" -p {port} " not in line or "postgres" not in line:
            continue
        parts = line.split(None, 3)
        if len(parts) >= 3 and parts[0].isdigit():
            found.append((int(parts[0]), line.strip()))
    return found


def capture_stall_evidence(port: int, admin_url: str) -> str | None:
    """Dump everything the host still knows about a stalled throwaway cluster:
    the postmasters' ps state, the postmaster stack (macOS `sample`), and any
    rows the admin connection still answers (`pg_stat_activity`/`pg_locks`).
    Writes into the fixture-log artifact dir (survives the tmpfs teardown).

    The file is written incrementally and the path returned before any slow
    step runs, so even a capture that itself hangs on the stalled cluster
    (e.g. `sample` against a stopped postmaster) leaves the earlier evidence
    on disk and a path the watchdog can report."""
    dest_dir = fixture_log_artifact_dir()
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(f"could not create stall-evidence dir {dest_dir}: {e!r}")
        return None
    import datetime

    evidence = dest_dir / f"stall-{port}.txt"

    def _append(lines: list[str]) -> None:
        try:
            with evidence.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError as e:
            logger.warning(f"could not write stall evidence {evidence}: {e!r}")

    _append(
        [
            f"throwaway PG stall evidence (port {port}) at "
            f"{datetime.datetime.now(datetime.UTC).isoformat()}Z"
        ]
    )
    for pid, state_line in postmaster_pids(port):
        _append([f"process: {state_line}"])
        if IS_MACOS:
            try:
                sample = subprocess.run(  # noqa: S603 -- host utility, fixed argv
                    ["sample", str(pid), "2", "1"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                _append([f"--- sample {pid} ---", sample.stdout[:8000]])
            except (OSError, subprocess.TimeoutExpired) as e:
                _append([f"(sample {pid} failed: {e!r})"])
    try:
        import psycopg

        with psycopg.connect(admin_url, connect_timeout=3) as conn, conn.cursor() as cur:
            for query in (
                "SELECT pid, state, wait_event_type, wait_event, query FROM pg_stat_activity",
                "SELECT locktype, mode, granted, pid FROM pg_locks LIMIT 200",
            ):
                try:
                    cur.execute(query)
                    _append([f"--- {query} ---"])
                    for row in cur.fetchall():
                        _append([str(row)])
                except Exception as e:
                    _append([f"(query failed: {e!r})"])
    except Exception as e:
        _append([f"(admin connection failed: {e!r})"])
    return str(evidence)


def kill_stalled_postmaster(port: int) -> None:
    """SIGKILL the postmasters serving `port` — the fail-fast half of the
    watchdog. Killing by PID from `ps` (anchored on the port the instance
    owns) never touches a real cluster: a real cluster's command line names
    its own home's port, which cannot equal a throwaway's OS-allocated port."""
    for pid, _state in postmaster_pids(port):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError as e:
            logger.warning(f"could not kill stalled postmaster {pid}: {e!r}")


class StallWatchdog(threading.Thread):
    """Daemon thread probing the throwaway cluster; on a sustained stall,
    captures evidence and kills the postmaster so the suite fails fast."""

    def __init__(self, admin_url: str, port: int) -> None:
        super().__init__(name=f"pg-stall-watchdog-{port}", daemon=True)
        self._admin_url = admin_url
        self._port = port
        self._stop = threading.Event()
        self.evidence_path: str | None = None

    def run(self) -> None:
        import psycopg

        failures = 0
        while not self._stop.wait(_PROBE_INTERVAL_S):
            try:
                with (
                    psycopg.connect(self._admin_url, connect_timeout=_PROBE_TIMEOUT_S) as conn,
                    conn.cursor() as cur,
                ):
                    cur.execute("SELECT 1")
                    cur.fetchone()
                failures = 0
            except Exception as e:
                failures += 1
                logger.warning(
                    f"throwaway PG probe {failures}/{_PROBE_CONSECUTIVE_FAILURES} failed "
                    f"(port {self._port}): {e!r}"
                )
                if failures >= _PROBE_CONSECUTIVE_FAILURES:
                    self.evidence_path = capture_stall_evidence(self._port, self._admin_url)
                    kill_stalled_postmaster(self._port)
                    return

    def stop(self) -> None:
        self._stop.set()


@contextmanager
def stall_guard(admin_url: str, port: int) -> Generator[None, None, None]:
    """Run the stall watchdog over a throwaway cluster's usable lifetime; if it
    fired (cluster stalled and was killed for fail-fast), log the evidence path
    at teardown so the failure's "why" survives next to its "what"."""
    watchdog = StallWatchdog(admin_url, port)
    watchdog.start()
    try:
        yield
    finally:
        watchdog.stop()
        if watchdog.evidence_path is not None:
            logger.error(
                f"throwaway PG (port {port}) stalled and was killed for fail-fast; "
                f"stall evidence: {watchdog.evidence_path}"
            )
