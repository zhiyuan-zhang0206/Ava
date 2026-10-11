"""Bound relay process launch and credential delivery after birth registration."""

from __future__ import annotations

import contextlib
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict

import psutil

from base.db import Database
from base.native_process.runtime_incarnation import RuntimeIncarnation

__all__ = ["register_relay", "spawn_codex_relay"]


def spawn_codex_relay(
    agent_id: int,
    lease_id: str,
    relay_token: str,
    thread_id: str,
    codex_remote: str | None,
    codex_home: str | None = None,
    register: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> subprocess.Popen[bytes]:
    """Spawn the bound relay; the scoped credential travels over a private pipe.

    The token never appears in argv or the environment. The child receives the
    session environment projection and boots its cluster connections. Stdout is
    discarded because delivery uses the app-server Steer path; stderr flows
    into this process's log.
    """
    from base.sessions.env_forwarding import forward_env_dict

    relay_env = forward_env_dict()
    if codex_home is not None:
        relay_env["CODEX_HOME"] = codex_home
    argv = [
        sys.executable,
        "-m",
        "cli",
        "impersonate",
        "relay",
        str(agent_id),
        "--lease-id",
        lease_id,
        "--provider",
        "codex",
        "--thread-id",
        thread_id,
        "--token-stdin",
    ]
    if codex_remote is not None:
        argv.extend(["--codex-remote", codex_remote])
    process = subprocess.Popen(  # noqa: S603 — fixed argv built from constants and lease-row fields
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        start_new_session=True,
        env=relay_env,
    )
    with contextlib.suppress(BrokenPipeError, OSError):  # child already gone; poll reports it
        assert process.stdin is not None  # noqa: S101 — PIPE requested above
        with process.stdin:
            if register is not None:
                try:
                    register(process)
                except Exception:
                    process.terminate()
                    process.wait(timeout=5)
                    raise
            process.stdin.write(relay_token.encode() + b"\n")
    return process


def register_relay(
    db: Database,
    lease_id: str,
    incarnation: RuntimeIncarnation,
    token: str,
    process: subprocess.Popen[bytes],
    *,
    read_local_machine: Callable[[], str],
) -> None:
    from base.agents.impersonation.relay import record_relay_identity
    from base.native_process.ownership import OwnedProcess

    identity = OwnedProcess.capture(psutil.Process(process.pid))
    record_relay_identity(
        db, lease_id, incarnation, token, asdict(identity), local_machine=read_local_machine()
    )
