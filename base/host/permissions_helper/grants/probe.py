from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from base.host.permissions_helper.grants.catalog import (
    CATALOG,
    PING_KEYS,
    PREFLIGHT_SERVICES,
    Grant,
    PermissionItem,
    RequestMode,
)
from base.host.permissions_helper.wire import RequestGrantsResult

PROBE_WAIT_S = 10.0

POLL_S = 0.25

PREFLIGHT_CHILD = Path(__file__).with_name("children") / "preflight.py"

_RESULT_WORDS: dict[int, Grant] = {
    0: Grant.GRANTED,
    1: Grant.DENIED,
    2: Grant.NOT_DETERMINED,
}


class HelperChannel(Protocol):
    def ping(self) -> Mapping[str, object]: ...

    def request_grants(self) -> RequestGrantsResult: ...

    def spawn_process(
        self,
        name: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str,
        stdout: str,
        stderr: str,
    ) -> Mapping[str, object]: ...


class PermissionsProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProbeResult:
    grants: Mapping[str, Grant]
    booleans: Mapping[str, bool] = field(default_factory=dict[str, bool])
    helper_reachable: bool = True
    supports_request: bool = False
    diagnostic: str = ""
    unread: tuple[str, ...] = field(default_factory=tuple)

    def grant_for(self, item: PermissionItem) -> Grant:
        if item.mode is RequestMode.LIVE_EVENT:
            return Grant.UNKNOWN
        record = self._record_grant(item)
        if not item.ping_key:
            return record
        held = self.booleans.get(item.ping_key)
        if held is None:
            return record
        if held:
            return Grant.GRANTED
        return Grant.STALE if record is Grant.GRANTED else record

    def _record_grant(self, item: PermissionItem) -> Grant:
        observed = [self.grants.get(service, Grant.UNKNOWN) for service in item.services]
        if not observed:
            return Grant.UNKNOWN
        if all(grant is Grant.GRANTED for grant in observed):
            return Grant.GRANTED
        for grant in (Grant.DENIED, Grant.NOT_DETERMINED):
            if grant in observed:
                return grant
        return Grant.UNKNOWN


def spawn_child(
    client: HelperChannel,
    *,
    name: str,
    argv: list[str],
    env: dict[str, str],
    workdir: Path,
    tag: str,
) -> int:
    from base.host.permissions_helper.client import PermissionsHelperError

    try:
        result = client.spawn_process(
            name,
            argv,
            env,
            str(workdir),
            str(workdir / f"{tag}.stdout.log"),
            str(workdir / f"{tag}.stderr.log"),
        )
    except PermissionsHelperError as exc:
        message = str(exc)
        if "unknown method" in message:
            raise PermissionsProbeError(
                "helper build needs a rebuild: it does not support the spawn"
                f" wire method ({message!r}) -- rebuild or update the"
                " permissions helper, then re-run."
            ) from exc
        raise PermissionsProbeError(f"helper spawn failed: {message}") from exc
    pid = result.get("pid")
    if not isinstance(pid, int):
        raise PermissionsProbeError(f"helper spawn returned no pid: {result!r}")
    return pid


def read_result(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return None


# meiva:ref base/host/permissions_helper/grants/docs/grants.meiva.okf.md#one-probe-for-every-surface
def run_preflight(
    client: HelperChannel,
    *,
    workdir: Path,
    run_id: str,
    env: dict[str, str],
    services: tuple[str, ...],
    wait_s: float = PROBE_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Grant]:
    workdir.mkdir(parents=True, exist_ok=True)
    result_path = workdir / f"preflight-{run_id}.results.json"
    result_path.unlink(missing_ok=True)
    spawn_child(
        client,
        name=f"tcc-preflight-{run_id}",
        argv=[sys.executable, str(PREFLIGHT_CHILD), json.dumps(list(services)), str(result_path)],
        env=env,
        workdir=workdir,
        tag=f"preflight-{run_id}",
    )
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        text = read_result(result_path)
        if text is not None:
            data = json.loads(text)
            return {
                service: _RESULT_WORDS.get(entry["code"], Grant.UNKNOWN)
                for service, entry in data.items()
            }
        sleep(POLL_S)
    raise PermissionsProbeError(
        f"preflight probe produced no result within {wait_s:.0f}s"
        f" (see the child logs under {workdir})"
    )


def probe(
    client: HelperChannel,
    *,
    workdir: Path,
    run_id: str,
    env: dict[str, str],
    services: tuple[str, ...] | None = None,
    wait_s: float = PROBE_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
) -> ProbeResult:
    from base.host.permissions_helper.client import PermissionsHelperError

    try:
        ping = client.ping()
    except PermissionsHelperError as exc:
        return ProbeResult(
            grants={},
            helper_reachable=False,
            diagnostic=(
                f"The permissions helper did not answer ({exc}), so no grant could be read."
                " This is a helper liveness problem: check its launchd job"
                " (`launchctl list | grep com.meiva.permissions-helper`) and"
                " $MEIVA_HOME/logs/permissions-helper.log."
            ),
        )
    grants = run_preflight(
        client,
        workdir=workdir,
        run_id=run_id,
        env=env,
        services=services or PREFLIGHT_SERVICES,
        wait_s=wait_s,
        sleep=sleep,
    )
    booleans: dict[str, bool] = {}
    for key in PING_KEYS:
        value = ping.get(key)
        if isinstance(value, bool):
            booleans[key] = value
    return ProbeResult(
        grants=grants,
        booleans=booleans,
        supports_request=ping.get("request_grants_v1") is True,
        unread=tuple(item.id for item in CATALOG if item.mode is RequestMode.LIVE_EVENT),
    )


# meiva:ref base/host/permissions_helper/grants/docs/grants.meiva.okf.md#requests-run-in-the-helper
def request_process_grants(client: HelperChannel) -> RequestGrantsResult:
    from base.host.permissions_helper.client import PermissionsHelperError

    try:
        return client.request_grants()
    except PermissionsHelperError as exc:
        raise PermissionsProbeError(f"grant request failed: {exc}") from exc
