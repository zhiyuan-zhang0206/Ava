"""dev startup entry — spawn a root agent process and print its id.

Production spawn goes through the SDK (`ava.agents.spawn(...)`) or the
gateway endpoint; this script is the "first time starting up / no root
agent running" bootstrap entry (chicken-and-egg solution — how do you
start the first agent when the agents_meta table is empty).

    .venv/bin/python scripts/start_agent.py

Only launches the process; **does not deliver an inbound** — after spawn,
the agent idles waiting for inbound. To make it do work, use the web UI
to send a message to the printed agent_id.

**Gateway must be up first** — the bootstrap also goes through HTTP
`POST /api/agents`, the same path as the SDK / frontend. Gateway is
centralized at the gateway.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.http_dial import post as dial_post
from shared.machine import gateway_api_base, gateway_auth_headers


def main() -> None:
    base = gateway_api_base()  # role-blind: the configured gateway_url (localhost on a dev cluster)
    # /api/agents is an authenticated route: present the cluster secret as a
    # Bearer token (empty dict in dev/test where no secret is set). Without it a
    # multi-host gateway rejects this bootstrap spawn with 401.
    resp = dial_post(f"{base}/api/agents", json={"spawner": "user"}, headers=gateway_auth_headers())
    resp.raise_for_status()
    agent_id = int(resp.json()["id"])
    print(f"agent_id={agent_id}")


if __name__ == "__main__":
    main()
