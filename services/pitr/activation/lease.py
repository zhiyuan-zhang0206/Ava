"""Owned deployment-lease renewal for long PITR activation work."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from base.deploy.progress_timeout import LEASE_RENEW_INTERVAL_S
from base.deploy.state.cluster_lock import lease_may_lapse, renew_update_lock
from base.log import logger


def run_while_renewing[T](holder: str, action: Callable[[threading.Event], T]) -> T:
    """Run `action` while renewing `holder`'s just-acquired deployment lease.

    A renewal that raises is a missed round, retried until the lease could
    lapse before the next one (`lease_may_lapse`); a renewal answered "not
    yours", or failures lasting that long, lose the lease and stop `action`.
    """
    stop = threading.Event()
    finished = threading.Event()
    lost = threading.Event()
    ready = threading.Event()

    def renew() -> None:
        renewed = time.monotonic()
        while not finished.is_set():
            try:
                owned = renew_update_lock(holder)
            except Exception as exc:
                logger.warning("[pitr] deployment lease renewal missed: {exc}", exc=exc)
                owned = None if not lease_may_lapse(time.monotonic() - renewed) else False
            if owned is False:
                lost.set()
                stop.set()
                ready.set()
                return
            if owned:
                renewed = time.monotonic()
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
