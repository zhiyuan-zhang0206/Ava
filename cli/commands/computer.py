"""`ava computer` — operator commands for the computer-use daemon.

Today one verb: `release` — the operator's last resort to force-release the
screen when a session is wedged (a holder that stopped acting but whose lease
has not expired yet). The next FIFO waiter takes over immediately. This talks
the daemon's line protocol directly (no agent identity — it is an operator
action, so it is logged by the daemon but not audited as a computer_action).
"""

from __future__ import annotations

import json
import socket
import sys


def h_computer_release(args: object) -> int:
    """Force-release the screen from its current holder."""
    del args  # no flags today
    from shared.paths import computer_mcp_socket

    sock_path = str(computer_mcp_socket())
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(sock_path)
    except (FileNotFoundError, ConnectionRefusedError) as e:
        print(f"computer-mcp daemon not reachable at {sock_path}: {e}", file=sys.stderr)
        return 1
    with s:
        stream = s.makefile("rwb")
        req = {
            "id": 1,
            "method": "call_tool",
            "tool": "release_control",
            "args": {"force": True},
        }
        stream.write((json.dumps(req) + "\n").encode())
        stream.flush()
        line = stream.readline()
    if not line:
        print("computer-mcp daemon closed the connection", file=sys.stderr)
        return 1
    try:
        resp = json.loads(line)
    except json.JSONDecodeError:
        print("computer-mcp daemon returned a corrupted response", file=sys.stderr)
        return 1
    if not resp.get("ok"):
        print(f"release failed: {resp.get('error', 'unknown error')}", file=sys.stderr)
        return 1
    try:
        result = json.loads(resp["result"]["content"][0]["text"])
    except (KeyError, TypeError, json.JSONDecodeError):
        print("computer-mcp daemon returned an unexpected release payload", file=sys.stderr)
        return 1
    released = result.get("released")
    if released is not None:
        print(f"released the screen from agent {released}")
    else:
        print("screen was free (no holder)")
    return 0
