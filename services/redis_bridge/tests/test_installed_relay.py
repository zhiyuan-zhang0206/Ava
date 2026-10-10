"""Run the single copied module under the actual installed stdlib interpreter."""

from __future__ import annotations

import json
import select
import shutil
import socket
import subprocess
from pathlib import Path

from services.redis_bridge import relay


def test_installed_source_forwards_and_sigterm_reclaims_idle_connection(tmp_path: Path) -> None:
    installed = tmp_path / "relay.py"
    shutil.copyfile(relay.__file__, installed)
    with socket.socket() as reservation, socket.socket() as backend_listener:
        reservation.bind(("127.0.0.1", 0))
        listen_port = reservation.getsockname()[1]
        reservation.close()
        backend_listener.bind(("127.0.0.1", 0))
        backend_listener.listen()
        backend_listener.settimeout(3.0)
        argv = [
            "relay.py",
            "--listen-host",
            "127.0.0.1",
            "--listen-port",
            str(listen_port),
            "--backend-port",
            str(backend_listener.getsockname()[1]),
        ]
        process = subprocess.Popen(
            [
                "/usr/bin/python3",
                "-c",
                "import json, os, runpy, sys; "
                "sys.argv = json.loads(os.environ['RELAY_TEST_ARGV']); "
                "runpy.run_path('relay.py', run_name='__main__')",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=tmp_path,
            # Do not permit repository packages or its virtualenv to help imports.
            env={"PATH": "/usr/bin:/bin", "RELAY_TEST_ARGV": json.dumps(argv)},
        )
        try:
            assert process.stdout is not None
            assert select.select([process.stdout], [], [], 3.0)[0], "relay never became ready"
            assert "relay listening" in process.stdout.readline()
            assert not select.select([process.stdout], [], [], 0.35)[0], (
                "an idle Python 3.9 listener timeout must not trigger rebind"
            )
            with socket.create_connection(("127.0.0.1", listen_port), timeout=3.0) as client:
                backend, _ = backend_listener.accept()
                with backend:
                    client.sendall(b"PING\r\n")
                    assert backend.recv(6) == b"PING\r\n"
                    backend.sendall(b"PONG\r\n")
                    assert client.recv(6) == b"PONG\r\n"
                    process.terminate()
                    _stdout, stderr = process.communicate(timeout=3.0)
                    assert process.returncode == 0, stderr
                    assert client.recv(1) == b""
                    assert backend.recv(1) == b""
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3.0)
