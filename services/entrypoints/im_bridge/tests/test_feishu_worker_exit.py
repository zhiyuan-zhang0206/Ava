"""The production IM main exits while Feishu's SDK call remains blocked."""

import asyncio
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from services.entrypoints.im_bridge import daemon
from services.entrypoints.im_bridge.adapters.tests.test_feishu_adapter import (
    FakeCore,
    FakeWsClient,
    PatchingAdapter,
)
from services.entrypoints.im_bridge.tests.slices import feishu_config
from tests.components.services.daemon_shutdown_test_support import Child

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGTERM contract")


def run_blocked_ws_child(markers_path: Path) -> None:
    """Use a blocked SDK transport with the real adapter and daemon main."""

    def mark(name: str) -> None:
        with markers_path.open("a", encoding="utf-8") as markers:
            markers.write(f"{name}\n")

    class BlockedWsClient(FakeWsClient):
        def start(self) -> None:
            loop = asyncio.get_event_loop()
            loop.call_soon(mark, "ready")
            try:
                loop.run_forever()
            finally:
                mark("worker-exited")

        async def _disconnect(self) -> None:
            mark("disconnect-requested")

    async def run_ws() -> None:
        adapter = PatchingAdapter(
            FakeCore(),
            feishu_config(
                feishu_app_id="cli_x",
                feishu_app_secret=secrets.token_urlsafe(16),
                feishu_poll_interval_seconds=0,
            ),
            BlockedWsClient(),
        )
        try:
            async with asyncio.TaskGroup() as tasks:
                try:
                    await adapter.start(tasks)
                    await asyncio.Event().wait()
                finally:
                    adapter.begin_shutdown()
        finally:
            await adapter.stop()
            await asyncio.sleep(0)
            mark("cleanup-ran")

    daemon.run = run_ws
    daemon.main()


def test_sigterm_exits_while_feishu_sdk_worker_remains_blocked(tmp_path: Path) -> None:
    markers_path, log_path = tmp_path / "markers.txt", tmp_path / "im-feishu.log"
    home = tmp_path / "ava-home"
    home.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        endpoint = f"http://127.0.0.1:{sock.getsockname()[1]}"
    (home / ".env").write_text(
        f"AVA_TELEMETRY_OTLP_ENDPOINT={endpoint}\nAVA_MACHINE_NAME=feishu-shutdown-test\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "AVA_HOME": str(home),
        "AVA_TELEMETRY_OTLP_ENDPOINT": endpoint,
        "DAEMON_SHUTDOWN_TEST_MARKERS": str(markers_path),
    }
    env.pop("AVA_PERMISSIONS_HELPER_PID", None)
    log_file = log_path.open("wb")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os; from pathlib import Path; "
            "from services.entrypoints.im_bridge.tests.test_feishu_worker_exit "
            "import run_blocked_ws_child; "
            "run_blocked_ws_child(Path(os.environ['DAEMON_SHUTDOWN_TEST_MARKERS']))",
        ],
        cwd=Path(__file__).resolve().parents[4],
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    child = Child(proc, markers_path, log_path, log_file)
    try:
        child.wait_marker("ready")
        child.terminate()
        child.wait_bounded_exit(what="blocked Feishu SDK worker")
        assert "[im_bridge] interrupted" in child.log_tail(), child.log_tail()
        assert "cleanup-ran" in child.markers(), child.log_tail()
        assert "disconnect-requested" in child.markers(), child.log_tail()
        assert "worker-exited" not in child.markers()
    finally:
        child.close()
