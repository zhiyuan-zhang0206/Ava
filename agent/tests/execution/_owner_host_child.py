"""A disposable host for one exec domain owner; never imported by a production entry.

Run as ``python -I -B _owner_host_child.py <owner-context-path>``. It starts the owner, permits its
request once the owner is ready, then idles until the test kills it.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from base.agents.incarnation.exec_owner_protocol import OwnerControl, OwnerReady, read_owner_context

path = Path(sys.argv[1])
context = read_owner_context(path)
owner = subprocess.Popen(  # noqa: S603 -- fixed argv, this interpreter runs the owner module
    [
        sys.executable,
        "-I",
        "-B",
        "-X",
        "utf8",
        "-m",
        "agent.execution.domain_owner",
        "--context",
        str(path),
    ],
    stdin=subprocess.PIPE,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=True,
)
while not path.with_suffix(".ready").exists():
    if owner.poll() is not None:
        raise RuntimeError("owner exited before handshake")
    time.sleep(0.01)
ready = OwnerReady.model_validate_json(path.with_suffix(".ready").read_bytes())
assert owner.stdin is not None
owner.stdin.write(
    OwnerControl(request=ready.allocation.request, domain=ready.allocation.domain, action="permit")
    .model_dump_json()
    .encode()
    + b"\n"
)
owner.stdin.flush()
while True:
    time.sleep(1)
