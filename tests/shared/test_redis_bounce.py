"""End-to-end: a parked `RedisInboundListener` survives a real redis-server
bounce (process killed, then restarted) and still wakes on a post-restart
publish.

The existing `tests/shared/test_redis_listener.py::test_reconnect_after_close`
only closes the *client* connection — the server stays up, so reconnect is
immediate. This exercises the harder real-outage shape: the server process dies
under a parked listener (the connection is severed mid-`get_message`), stays down
across the listener's backoff, then comes back on the same port. The listener
must reconnect + re-subscribe and deliver a wake published after it recovers.

Runs its own throwaway redis on a fixed port (so restart reuses the URL), never
the session redis — killing the shared one would break every other test.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
import redis

from shared.cluster import inbound_channel
from shared.redis_listener import RedisInboundListener


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_redis(port: int, datadir: Path) -> subprocess.Popen[bytes]:
    """Start a throwaway redis-server on `port`, wait until it accepts connections."""
    proc = subprocess.Popen(  # noqa: S603 — static argv, throwaway server
        [
            "redis-server",
            "--port",
            str(port),
            "--bind",
            "127.0.0.1",
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(datadir),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return proc
        time.sleep(0.05)
    proc.kill()
    raise RuntimeError(f"throwaway redis did not start on :{port}")


def _stop_redis(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=10)
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


@pytest.fixture
def bounceable_redis(tmp_path: Path) -> Iterator[tuple[str, Path, int]]:
    """A throwaway redis process the test can kill + restart on a fixed port.
    Yields (url, datadir, port); the datadir/port let the test bring it back at
    the same URL. Torn down on exit."""
    port = _free_port()
    datadir = tmp_path / "redis"
    datadir.mkdir()
    proc = _start_redis(port, datadir)
    url = f"redis://127.0.0.1:{port}/0"
    try:
        yield url, datadir, port
    finally:
        _stop_redis(proc)
        shutil.rmtree(datadir, ignore_errors=True)


@pytest.mark.flaky  # real redis-server SIGKILL bounce + reconnect-backoff timing (serial bucket)
async def test_parked_listener_wakes_after_redis_bounce(
    bounceable_redis: tuple[str, Path, int],
) -> None:
    """kill redis → parked listener → restart → publish → wake."""
    url, datadir, port = bounceable_redis
    agent_id = 6001
    listener = RedisInboundListener(url, agent_id)
    server = _start_redis  # bind for readability

    # 1. Park the listener on a long wait; let it subscribe.
    wait_task = asyncio.create_task(listener.wait_one(timeout=30.0))
    await asyncio.sleep(0.4)
    assert not wait_task.done(), "listener should be parked, not already returned"

    # 2. Kill redis under the parked listener (severs its subscribed connection).
    with redis.Redis.from_url(url) as probe:  # pyright: ignore[reportUnknownMemberType]
        probe_pid = probe.info("server")["process_id"]  # pyright: ignore[reportUnknownMemberType]
    os.kill(probe_pid, signal.SIGKILL)
    # Give the client a beat to notice the dropped connection and enter backoff.
    await asyncio.sleep(0.5)
    assert not wait_task.done(), "a severed connection must not resolve the wait"

    # 3. Bring redis back on the SAME port (same URL the listener retries).
    proc = server(port, datadir)
    try:
        # 4. Publish repeatedly until the reconnected+resubscribed listener wakes.
        #    pub/sub has no retention, so a publish before re-subscribe is lost;
        #    repeating guarantees one lands after recovery.
        async def _publish_until_woken() -> None:
            while not wait_task.done():
                with redis.Redis.from_url(url) as r:  # pyright: ignore[reportUnknownMemberType]
                    r.publish(inbound_channel(agent_id), "42")  # pyright: ignore[reportUnknownMemberType]
                await asyncio.sleep(0.25)

        pub_task = asyncio.create_task(_publish_until_woken())
        try:
            await asyncio.wait_for(wait_task, timeout=20.0)
        except TimeoutError:
            pytest.fail("listener never woke after redis came back — reconnect/resubscribe broken")
        finally:
            pub_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pub_task
    finally:
        _stop_redis(proc)
        await listener.close()
