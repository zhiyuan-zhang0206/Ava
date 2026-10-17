from __future__ import annotations

import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from base.host.permissions_helper.grants import gate, probe, triggers
from base.host.permissions_helper.grants.catalog import Grant
from base.host.permissions_helper.grants.gate import GateReport, GateState
from base.host.permissions_helper.grants.probe import HelperChannel
from base.paths import meiva_home

REQUEST_TIMEOUT_S = 90.0


@dataclass(frozen=True)
class GateHost:
    client: HelperChannel
    env: dict[str, str]
    workdir: Path
    home: Path
    record_path: Path
    platform: str


def scratch_dir() -> Path:
    return meiva_home() / "permissions-onboarding"


def child_env() -> dict[str, str]:
    from base.sessions import env_forwarding

    return env_forwarding.forward_env_dict(activate_venv=False)


def helper_client() -> HelperChannel:
    from base.sessions.helperproc import helper_client as sessions_helper_client

    return sessions_helper_client()


def local_host() -> GateHost:
    return GateHost(
        client=helper_client(),
        env=child_env(),
        workdir=scratch_dir(),
        home=Path.home(),
        record_path=gate.record_path(),
        platform=sys.platform,
    )


# meiva:ref services/desktop/permissions_helper/docs/onboarding.meiva.okf.md#request-is-not-evidence
def check(
    *,
    host: GateHost | None = None,
    now: datetime | None = None,
    run_id: str | None = None,
) -> GateReport:
    seat = host or local_host()
    if seat.platform != "darwin":
        return gate.not_applicable(now)
    record = gate.read_record(seat.record_path)
    deferred = bool(record.deferred) if record is not None else False
    result = probe.probe(
        seat.client,
        workdir=seat.workdir,
        run_id=run_id or uuid.uuid4().hex[:8],
        env=seat.env,
    )
    report = gate.evaluate(result, deferred=deferred, now=now)
    gate.remember(report, deferred=deferred, path=seat.record_path)
    return report


# meiva:ref services/desktop/permissions_helper/docs/onboarding.meiva.okf.md#no-dialog-without-a-human
def onboard(
    *,
    interactive: bool,
    host: GateHost | None = None,
    now: datetime | None = None,
    timeout_s: float = REQUEST_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
    echo: Callable[[str], None] | None = None,
    request_item: Callable[..., str] = triggers.request_item,
) -> GateReport:
    seat = host or local_host()
    report = check(host=seat, now=now)
    if not interactive or report.state in (GateState.COMPLETE, GateState.NOT_APPLICABLE):
        return report
    pending = report.prompt_now()
    if not pending:
        return report
    seat.workdir.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:8]
    for item in pending:
        if echo is not None:
            echo(f"Asking for: {item.title}")
        verdict = request_item(
            seat.client,
            item,
            workdir=seat.workdir,
            run_id=run_id,
            env=seat.env,
            home=seat.home,
            timeout_s=timeout_s,
            sleep=sleep,
        )
        if echo is not None:
            echo(f"  -> {verdict}")
    return check(host=seat, now=now)


def observations(report: GateReport) -> dict[str, str]:
    return {status.item.id: status.grant.value for status in report.items}


def deferred_flag(*, host: GateHost | None = None) -> bool:
    seat = host or local_host()
    record = gate.read_record(seat.record_path)
    return bool(record.deferred) if record is not None else False


def defer(*, now: datetime | None = None, host: GateHost | None = None) -> None:
    seat = host or local_host()
    gate.mark_deferred(now=now, path=seat.record_path)


def restart_helper(
    *, run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run
) -> str:
    from services.desktop.permissions_helper.launchd_job import (
        helper_job_domain,
        helper_job_label,
    )

    target = f"{helper_job_domain()}/{helper_job_label()}"
    done = run(["launchctl", "kickstart", "-k", target], capture_output=True)
    if done.returncode != 0:
        detail = (done.stderr or b"").decode("utf-8", "replace").strip()
        return f"could not restart the permissions helper ({target}): {detail or done.returncode}"
    return f"restarted the permissions helper ({target}); its grants were re-read."


def missing_summary(report: GateReport) -> str:
    if report.state is GateState.UNAVAILABLE:
        return report.diagnostic
    if not report.missing:
        return ""
    names = ", ".join(status.item.title for status in report.missing)
    return f"{len(report.missing)} permission(s) still missing: {names}"


def grant_values(report: GateReport) -> dict[str, Grant]:
    return {status.item.id: status.grant for status in report.items}


class GateService:
    def check(self) -> GateReport:
        return check()

    def onboard(
        self,
        *,
        interactive: bool,
        timeout_s: float = REQUEST_TIMEOUT_S,
        echo: Callable[[str], None] | None = None,
    ) -> GateReport:
        return onboard(interactive=interactive, timeout_s=timeout_s, echo=echo)

    def defer(self) -> None:
        defer()

    def restart_helper(self) -> str:
        return restart_helper()
