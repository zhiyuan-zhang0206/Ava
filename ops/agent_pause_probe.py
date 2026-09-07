"""Read the running local host's maintenance capability without trusting disk code."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.request import ProxyHandler, build_opener
from uuid import UUID

from shared.config import settings
from shared.daemon_health import health_port
from shared.paths import ava_home


@dataclass(frozen=True)
class HostIdentity:
    owner: UUID
    active: frozenset[int]


def host_running() -> bool:
    """A down recorded service has no hosted work; reject live unrecorded owners."""
    import psutil

    from shared.cluster import session_name
    from shared.session_backend import get_backend

    if get_backend().has_session(session_name("agent-host")):
        return True
    path = Path(settings.services.agent_host_pidfile)
    if path.exists():
        pid = int(path.read_text().strip())
        if psutil.pid_exists(pid):
            raise RuntimeError("agent-host PID exists without its owned service session")
    # A missing record is not evidence that a daemon which lost that record
    # exited. Check the stable launch module and its private home identity too.
    home = ava_home().resolve()
    for process in psutil.process_iter(["pid", "cmdline"]):
        argv = cast(list[str], process.info["cmdline"] or [])
        if not any(
            argv[i : i + 2] == ["-m", "services.agent_host.daemon"] for i in range(len(argv) - 1)
        ):
            continue
        try:
            raw_home = process.environ().get("AVA_HOME")
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise RuntimeError("cannot identify an unrecorded agent-host home") from exc
        if raw_home is None or Path(raw_home).resolve() == home:
            raise RuntimeError("agent-host is still running without its service record")
    return False


def host_identity() -> HostIdentity:
    """Refuse an old daemon, a foreign home, or a response from another PID."""
    port = health_port("agent_host")
    with build_opener(ProxyHandler({})).open(
        f"http://127.0.0.1:{port}/stats", timeout=5
    ) as response:
        raw: object = json.loads(response.read(65537))
    if not isinstance(raw, dict):
        raise TypeError("agent-host did not return an identity object")
    data = cast(dict[str, object], raw)
    if data["maintenance_protocol"] != 1 or data["home"] != str(ava_home()):
        raise RuntimeError("running agent-host does not support maintenance for this home")
    pid = int(Path(settings.services.agent_host_pidfile).read_text().strip())
    if type(data["pid"]) is not int or data["pid"] != pid:
        raise RuntimeError("agent-host maintenance response does not match its pidfile")
    active = data["active_agents"]
    if not isinstance(active, list) or any(
        type(item) is not int for item in cast(list[object], active)
    ):
        raise TypeError("invalid active-agent set from agent-host")
    owner = data["runtime_owner"]
    if not isinstance(owner, str):
        raise TypeError("missing agent-host boot owner")
    return HostIdentity(UUID(owner), frozenset(cast(list[int], active)))


def ops_quiescent(timeout: float) -> None:
    """Wait for admitted HTTP requests and actual executor work, after closing admission."""
    import time

    from shared.cluster import session_name
    from shared.session_backend import get_backend

    if not get_backend().has_session(session_name("ops")):
        return
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("ops still has admitted work; maintenance hold retained")
        with build_opener(ProxyHandler({})).open(
            f"http://127.0.0.1:{health_port('ops')}/healthz", timeout=min(5, remaining)
        ) as response:
            data = json.loads(response.read(65537))
        if data["home"] != str(ava_home()):
            raise RuntimeError("ops health belongs to another home")
        if data["pid"] != int(settings.services.ops_pidfile.read_text().strip()):
            raise RuntimeError("ops health does not match its recorded process")
        progress = data["maintenance"]
        if progress["protocol"] != 1:
            raise RuntimeError("running ops has no maintenance request accounting")
        if progress["requests"] == 0 and progress["workers"] == 0:
            return
        time.sleep(min(0.1, remaining))
