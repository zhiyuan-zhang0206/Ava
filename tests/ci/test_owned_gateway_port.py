"""Actual inherited-socket transport; full gateway lifespan remains in E2E CI."""

import errno
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil
import pytest

from tests.e2e._ports import GATEWAY_PORT, GATEWAY_SOCKET
from tests.e2e._proc import listener_evidence, managed_proc, require_native_listener


def test_owned_port_survives_delay_and_real_uvicorn_restart(tmp_path: Path) -> None:
    app = tmp_path / "socket_app.py"
    app.write_text(
        "import os\n"
        "async def app(scope, receive, send):\n"
        "    await send({'type':'http.response.start','status':200,'headers':[]})\n"
        "    await send({'type':'http.response.body','body':str(os.getpid()).encode()})\n"
    )
    receipts: list[dict[str, object]] = []
    for iteration in range(2):
        with socket.socket() as competitor, pytest.raises(OSError) as collision:
            competitor.bind(("127.0.0.1", GATEWAY_PORT))
        assert collision.value.errno == errno.EADDRINUSE
        time.sleep(0.1)  # Simulate the build/start gap while the actual owner retains the FD.
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "socket_app:app",
            "--app-dir",
            str(tmp_path),
            "--fd",
            str(GATEWAY_SOCKET.fileno()),
            "--lifespan",
            "off",
        ]
        with managed_proc(
            command,
            label="owned-port-proof",
            pass_fds=(GATEWAY_SOCKET.fileno(),),
            log_path=str(tmp_path / f"server-{iteration}.log"),
        ) as process:
            identity = psutil.Process(process.pid)
            birth = identity.create_time()
            deadline = time.monotonic() + 15
            while True:
                assert process.poll() is None
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{GATEWAY_PORT}", timeout=1
                    ) as reply:
                        assert int(reply.read()) == process.pid
                    break
                except (urllib.error.URLError, TimeoutError):
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
            require_native_listener(process.pid, birth, GATEWAY_PORT)
            with pytest.raises(RuntimeError, match="does not own"):
                require_native_listener(process.pid, birth - 1, GATEWAY_PORT)
            receipts.append(
                {
                    "pid": process.pid,
                    "birth": birth,
                    "listener": listener_evidence(GATEWAY_PORT, "transport-ready"),
                }
            )
        assert process.poll() is not None
    assert receipts[0]["pid"] != receipts[1]["pid"]
    output = os.environ.get("AVA_PORT_PROOF_OUTPUT")
    if output:
        Path(output).write_text(json.dumps(receipts, indent=2))


def test_released_allocation_is_not_ownership() -> None:
    with socket.socket() as allocation:
        allocation.bind(("127.0.0.1", 0))
        port = allocation.getsockname()[1]
    # Deterministic old-protocol counterexample: a different owner can bind it.
    with socket.socket() as competing_owner:
        competing_owner.bind(("127.0.0.1", port))
        with socket.socket() as old_gateway, pytest.raises(OSError) as collision:
            old_gateway.bind(("127.0.0.1", port))
        assert collision.value.errno == errno.EADDRINUSE
