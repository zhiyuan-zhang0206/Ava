"""Target-interpreter adapter for local previews; invoked by local.py only."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import TypedDict

import psutil


class TimelineItem(TypedDict):
    kind: str
    payload: str
    exec_ms: int | None


SERVICES = frozenset({"gateway", "frontend", "ops", "agent-host"})


def describe(run: Path, home: Path) -> None:
    from ops.roster import build_services
    from shared.cluster import get_record, record_app_port

    record = get_record(home)
    if record is None:
        raise RuntimeError("Start did not register this preview")
    config = {
        "services": sorted(SERVICES),
        "disabled_services": sorted({s.session for s in build_services()} - SERVICES),
        "ports": record.ports,
        "gateway_url": f"http://127.0.0.1:{record.ports['gateway']}",
        "frontend_url": f"http://127.0.0.1:{record_app_port(record)}",
    }
    (run / "config.json").write_text(json.dumps(config, indent=2) + "\n")


def check(run: Path, home: Path) -> None:
    import httpx

    from ops.roster import build_services

    config = json.loads((run / "config.json").read_text())
    specs = [s for s in build_services() if s.session in SERVICES]
    if frozenset(s.session for s in specs) != SERVICES:
        raise RuntimeError("Target revision does not implement the preview service profile")
    deadline = time.monotonic() + 240
    failures = []
    while time.monotonic() < deadline:
        failures = [
            s.session for s in specs if s.identity_probe is None or not s.identity_probe().alive
        ]
        if not failures:
            response = httpx.get(config["frontend_url"], timeout=10)
            response.raise_for_status()
            (run / "check.json").write_text(
                json.dumps(
                    {
                        "services": sorted(SERVICES),
                        "frontend_http": response.status_code,
                        "home": str(home),
                    },
                    indent=2,
                )
                + "\n"
            )
            return
        time.sleep(1)
    raise RuntimeError(f"Services not ready: {failures}")


def smoke(run: Path) -> None:
    import httpx

    from tests.e2e.fakes.scenarios.message_flow import REPLY_TEXT

    config = json.loads((run / "config.json").read_text())
    with httpx.Client(base_url=config["gateway_url"], timeout=30) as client:
        response = client.post(
            "/api/agents",
            json={"spawner": "user", "prompt": "Compute 1+2.", "prompt_source": "user"},
        )
        response.raise_for_status()
        agent = response.json()["id"]
        deadline = time.monotonic() + 120
        items = []
        while time.monotonic() < deadline:
            response = client.get(f"/api/agents/{agent}/timeline?limit=1000")
            response.raise_for_status()
            items = response.json()["items"]
            if execution_completed(items, REPLY_TEXT):
                (run / "smoke.json").write_text(
                    json.dumps(
                        {
                            "agent": agent,
                            "model": "scripted message_flow",
                            "timeline": items,
                        },
                        indent=2,
                    )
                    + "\n"
                )
                return
            time.sleep(1)
        raise RuntimeError(f"Agent {agent} did not complete scripted execution: {items}")


def execution_completed(items: list[TimelineItem], reply: str) -> bool:
    """Judge the executed body, never a digit in its timestamp or an LLM claim."""
    code = [i["payload"].strip() for i in items if i["kind"] == "agent_code"]
    replies = [i["payload"] for i in items if i["kind"] == "agent_chat"]
    outputs = [i for i in items if i["kind"] == "code_output"]
    if code != ["print(1 + 2)"] or reply not in replies or len(outputs) != 1:
        return False
    header, separator, body = outputs[0]["payload"].partition("\n\n")
    return bool(
        separator
        and body.strip() == "3"
        and outputs[0]["exec_ms"] is not None
        and "cancelled" not in header
        and "timeout" not in header
    )


def owned_processes(run: Path) -> list[psutil.Process]:
    """Find survivors even when a native daemon rewrites argv or loses its registry."""
    excluded = {os.getpid(), *(p.pid for p in psutil.Process().parents())}
    found: list[psutil.Process] = []
    for process in psutil.process_iter(["pid", "cmdline", "cwd", "status"]):
        if process.pid in excluded or process.info["status"] == psutil.STATUS_ZOMBIE:
            continue
        info = process.info
        cwd = Path(info["cwd"]) if info["cwd"] else None
        in_tree = cwd is not None and (cwd.is_relative_to(run))
        args: list[str] = info["cmdline"] or []
        if in_tree or any(arg == str(run) or arg.startswith(str(run) + "/") for arg in args):
            found.append(process)
    return found


def verify_stopped(run: Path, home: Path) -> None:
    survivors = [process.pid for process in owned_processes(run)]
    if survivors:
        raise RuntimeError(f"Preview processes survived stop: {survivors}")
    registry_path = run / "clusters.json"
    if registry_path.exists():
        registry = json.loads(registry_path.read_text())
        # The normal destroy entry releases the registry; observation never does.
        if str(home) in registry:
            raise RuntimeError("Preview registry slot remains after destroy")
    config_path = run / "config.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())
        for port in config["ports"].values():
            with socket.socket() as probe:
                probe.settimeout(0.2)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    raise RuntimeError(f"Port {port} is still listening after stop")
    (run / "cleanup.json").write_text(
        json.dumps({"processes": [], "listeners": [], "registry_released": True})
        + "\n"
    )


def main() -> None:
    run = Path(sys.argv[1]).resolve()
    home = run / "home"
    sys.path.insert(0, str(run / "source"))
    action = sys.argv[2]
    if action == "verify-stopped":
        verify_stopped(run, home)
        return
    from shared.paths import ava_home

    if ava_home().resolve() != home:
        raise RuntimeError("Target interpreter resolved a different home")
    if action == "describe":
        describe(run, home)
    elif action == "check":
        check(run, home)
    elif action == "smoke":
        smoke(run)
    else:
        raise ValueError(f"Unknown preview action: {action}")


if __name__ == "__main__":
    main()
