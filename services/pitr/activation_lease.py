"""Owned deployment-lease renewal for long PITR activation work."""

from __future__ import annotations

import threading
from collections.abc import Callable

from shared.cluster_lock import renew_update_lock
from shared.deploy_timing import LEASE_RENEW_INTERVAL_S


def run_while_renewing[T](holder: str, action: Callable[[threading.Event], T]) -> T:
    stop = threading.Event()
    finished = threading.Event()
    lost = threading.Event()
    ready = threading.Event()

    def renew() -> None:
        while not finished.is_set():
            try:
                owned = renew_update_lock(holder)
            except Exception:
                owned = False
            if not owned:
                lost.set()
                stop.set()
                ready.set()
                return
            ready.set()
            if finished.wait(LEASE_RENEW_INTERVAL_S):
                return

    worker = threading.Thread(target=renew, name="pitr-lease-renewer", daemon=True)
    worker.start()
    ready.wait()
    if lost.is_set():
        finished.set()
        worker.join()
        raise RuntimeError("PITR activation lost its deployment lease")
    try:
        result = action(stop)
    finally:
        finished.set()
        worker.join()
    if lost.is_set():
        raise RuntimeError("PITR activation lost its deployment lease")
    return result
