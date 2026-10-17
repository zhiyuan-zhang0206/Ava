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
    PREFLIGHT_SERVICES,
    Grant,
    PermissionItem,
    RequestMode,
)

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
    screen_recording: bool | None = None
    ax_trusted: bool | None = None
    helper_reachable: bool = True
    diagnostic: str = ""
    unread: tuple[str, ...] = field(default_factory=tuple)

    def grant_for(self, item: PermissionItem) -> Grant:
        if item.id == "screen-recording" and self.screen_recording is not None:
            return Grant.GRANTED if self.screen_recording else Grant.DENIED
        if item.id == "accessibility" and self.ax_trusted is not None:
            return Grant.GRANTED if self.ax_trusted else Grant.DENIED
        if item.mode is RequestMode.LIVE_EVENT:
            return Grant.UNKNOWN
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

    ping: Mapping[str, object] = {}
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
    screen = ping.get("preflight_screen")
    ax = ping.get("ax_trusted")
    return ProbeResult(
        grants=grants,
        screen_recording=screen if isinstance(screen, bool) else None,
        ax_trusted=ax if isinstance(ax, bool) else None,
        unread=tuple(item.id for item in CATALOG if item.mode is RequestMode.LIVE_EVENT),
    )
