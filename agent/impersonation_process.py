"""Bound relay process launch and credential delivery."""

from __future__ import annotations

import contextlib
import subprocess
import sys

__all__ = ["spawn_codex_relay"]


def spawn_codex_relay(
    agent_id: int,
    lease_id: str,
    relay_token: str,
    thread_id: str,
    codex_remote: str | None,
    codex_home: str | None = None,
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
            process.stdin.write(relay_token.encode() + b"\n")
    return process
