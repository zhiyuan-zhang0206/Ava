"""Detached agent-stack warm-up launcher, fired from `ava start`.

Spawns `agent.warmup` (see that module for what it warms) as a background,
best-effort process on agent-runner-capable hosts. Fire-and-forget: it never
blocks `ava start` and never raises -- a failed launch just means the first
agent pays the cold cost itself, as it does today.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from shared.machine import MachineRoles
from shared.paths import logs_dir


def _launch_agent_warmup(roles: MachineRoles, repo: Path) -> None:
    """Fire `agent.warmup` detached on agent-runner hosts.

    No-op on a gateway-only host (it runs no agents, so there is nothing to warm).
    Output goes to `$AVA_HOME/logs/warmup.log`; `start_new_session=True` detaches
    the child so it outlives the short-lived `ava start` process. Launched with
    `sys.executable` (the venv interpreter `ava start` itself runs under) rather
    than `uv run`, so no resident `uv` wrapper lingers behind the detached child.
    The child env comes from `agent_spawn_env_dict()` (the same AGENT_ENV_FORWARD_KEYS
    allowlist a real agent launch gets), so a warm-up process cannot pick up host
    env that a real agent would not see either.
    """
    if "agent-runner" not in roles:
        return
    from ops.agent_launch import agent_spawn_env_dict

    log_path = logs_dir() / "warmup.log"
    try:
        # The child dups this fd; the parent's copy is closed once Popen returns.
        log_fh = log_path.open("a")
        try:
            subprocess.Popen(
                [sys.executable, "-m", "agent.warmup"],
                cwd=str(repo),
                env=agent_spawn_env_dict(),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_fh.close()
        print(f"  + agent-stack warm-up (background, {log_path})")
    except Exception as e:
        print(f"  ✗ warm-up launch skipped: {e}")
