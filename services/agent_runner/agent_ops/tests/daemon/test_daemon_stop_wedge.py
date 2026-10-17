"""A stop stays bounded while `watch_to_user` and a blocked worker run (task #5129).

On 2026-10-14, the drain reached by a first SIGTERM was aborted when the
TaskGroup replayed the interrupt or when a retried SIGTERM arrived mid-drain,
skipping `_hard_exit` into interpreter teardown that hung joining default-executor
threads. This drives the real `daemon.main` stop path with `watch_to_user` and a
blocked worker thread, sending SIGTERM twice.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from base.deploy.tests.containers import free_port
from base.sessions.tests.poll_until import poll_until

_CHILD = Path(__file__).with_name("child_stop_blocked_watch.py")
_REPO = Path(__file__).resolve().parents[5]

# How long the child holds its drain open, so the retried stop lands inside it.
_HELD_S = 2.0
# The supervisor's stop window: the child must leave well inside it.
_STOP_WINDOW_S = 8.0


def _healthz_latency(port: int) -> float:
    started = time.monotonic()
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
        payload = json.loads(response.read())
    assert payload["name"] == "ops", payload
    return time.monotonic() - started


def test_a_stop_stays_bounded_while_the_watcher_and_blocked_worker_run(unit_home: Path) -> None:
    port = free_port()
    ready, blocked, draining, clean = (
        unit_home / name for name in ("ready", "blocked", "draining", "clean")
    )
    log = (unit_home / "child.log").open("w")
    env = dict(os.environ)
    env.update(MEIVA_HOME=str(unit_home), MEIVA_CONFIG_FETCH="skip")
    child = subprocess.Popen(  # noqa: S603 — fixed test-owned script
        [
            sys.executable,
            str(_CHILD),
            str(unit_home),
            str(port),
            str(ready),
            str(blocked),
            str(draining),
            str(clean),
            str(_HELD_S),
        ],
        cwd=_REPO,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        poll_until(
            lambda: ready.exists() and blocked.exists(),
            timeout=30,
            what="the daemon's health server up and the worker thread blocked",
        )
        # The watcher and blocked worker run off the event loop: /healthz answers.
        assert child.poll() is None
        latency = _healthz_latency(port)
        assert latency < 1.0, (
            f"healthz answered in {latency:.2f}s while the watcher and blocked worker ran"
        )
        assert not clean.exists()

        child.send_signal(signal.SIGTERM)
        poll_until(draining.exists, timeout=10, what="the drain's held leg")
        # A retried stop lands inside the held drain: it must not abort it.
        child.send_signal(signal.SIGTERM)

        started = time.monotonic()
        code = child.wait(timeout=_STOP_WINDOW_S + 5)
        elapsed = time.monotonic() - started
        assert elapsed < _STOP_WINDOW_S, f"the stop took {elapsed:.2f}s"
        assert code == 0, code
        assert clean.read_text() == "clean"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        log.close()
