"""Fleet update roster and smoke checks, shipped to the NEW gateway over stdin.

Keep this program in one source file: the updater sends this exact text, and
its regression tests execute the same module against current gateway APIs.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from typing import Any

from base.api_contracts.idempotency import PRINCIPAL_SCOPE, SCOPE_HEADER
from base.cluster.machine import gateway_api_base, gateway_auth_headers
from base.cluster.machine.boot import gateway_access_from_boot
from base.config import ConfigBoot
from base.host.net.http_dial import get, post


def completed(items: list[dict[str, Any]]) -> bool:
    """A successful code output must follow the requested smoke-test code."""
    code = [
        i
        for i, item in enumerate(items)
        if item["kind"] == "agent_code" and "print(1+2)" in item["payload"].replace(" ", "")
    ]
    outputs = (
        [item["payload"] for item in items[code[-1] + 1 :] if item["kind"] == "code_output"]
        if code
        else []
    )
    return bool(outputs) and outputs[0].partition("\n\n")[2].strip().startswith("3")


def main(argv: list[str]) -> int:
    access = gateway_access_from_boot(ConfigBoot())
    base, headers = gateway_api_base(access.gateway_url), gateway_auth_headers(access)
    if argv[0] == "roster":
        rows = get(f"{base}/api/cluster/roster?fresh=true", headers=headers, timeout=90)
        rows.raise_for_status()
        keep = (
            "name",
            "online",
            "identity_mismatch",
            "head_sha",
            "running_sha",
            "serve_agent_runner",
        )
        print(json.dumps([{key: row[key] for key in keep} for row in rows.json()]))
        return 0
    keyed = {**headers, "Idempotency-Key": uuid.uuid4().hex, SCOPE_HEADER: PRINCIPAL_SCOPE}
    spawned = post(
        f"{base}/api/keyed/v1/agents", headers=keyed, json={"machine": argv[1]}, timeout=60
    )
    out: dict[str, Any] = {"completed": False, "spawn": spawned.status_code}
    if spawned.status_code >= 300:
        print(json.dumps(out))
        return 1
    agent = out["agent"] = spawned.json()["id"]
    try:
        message = {
            "content": "Update smoke test. Run exactly this Python code with your code tool, once: print(1 + 2)",
            "source": "user",
        }
        post(
            f"{base}/api/agents/{agent}/messages", headers=headers, json=message, timeout=60
        ).raise_for_status()
        deadline = time.monotonic() + float(argv[2])
        while not out["completed"] and time.monotonic() < deadline:
            time.sleep(4)
            items = get(f"{base}/api/agents/{agent}/timeline", headers=headers, timeout=60).json()[
                "items"
            ]
            out["completed"] = completed(items)
    finally:
        out["terminate"] = post(
            f"{base}/api/agents/{agent}/terminate", headers=headers, timeout=60
        ).status_code
        print(json.dumps(out))
    return 0 if out["completed"] and out["terminate"] < 300 else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
