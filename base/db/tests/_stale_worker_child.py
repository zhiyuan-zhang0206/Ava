"""A process whose DB gate learns it is behind the minimum from a worker thread; never imported by production.

Run as ``python _stale_worker_child.py``; the gate must hard-exit the process instead of returning.
"""

from __future__ import annotations

import threading

from base.db.code_version_gate import ProcessDbGate

owner = ProcessDbGate(version=lambda: 1, process="worker-proof")
worker = threading.Thread(target=lambda: owner.observe_minimum(2))
worker.start()
worker.join(timeout=2)
raise RuntimeError("the stale worker did not terminate its process")
