"""dev startup entry — spawn a root agent process and print its id.

Production spawn goes through the SDK (`meiva.agents.spawn(...)`) or the
gateway endpoint; this script is the "first time starting up / no root
agent running" bootstrap entry (chicken-and-egg solution — how do you
start the first agent when the agents_meta table is empty).

    .venv/bin/python scripts/entrypoints/agent.py

Only launches the process; **does not deliver an inbound** — after spawn,
the agent idles waiting for inbound. To make it do work, use the web UI
to send a message to the printed agent_id.

**Gateway must be up first** — the bootstrap also goes through HTTP
`POST /api/keyed/v1/agents`, the same path as the SDK / frontend. Gateway is
centralized at the gateway.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from base.api_contracts.idempotency import PRINCIPAL_SCOPE, SCOPE_HEADER
from base.cluster.machine import gateway_api_base, gateway_auth_headers
from base.cluster.machine.ambient import gateway_access_from_settings
from base.host.net.http_dial import post as dial_post


def main() -> None:
    access = gateway_access_from_settings()  # ambient: O
    base = gateway_api_base(
        access.gateway_url
    )  # role-blind: the configured gateway_url (localhost on a dev cluster)
    # The keyed spawn route is authenticated: present the machine API token (or the
    # cluster secret) as a Bearer token; without it the gateway rejects this spawn with 401.
    headers = {
        **gateway_auth_headers(access),
        "Idempotency-Key": uuid.uuid4().hex,
        SCOPE_HEADER: PRINCIPAL_SCOPE,
    }
    resp = dial_post(f"{base}/api/keyed/v1/agents", json={"spawner": "user"}, headers=headers)
    resp.raise_for_status()
    agent_id = int(resp.json()["id"])
    print(f"agent_id={agent_id}")


if __name__ == "__main__":
    main()
