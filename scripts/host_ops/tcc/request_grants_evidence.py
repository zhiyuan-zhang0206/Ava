from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from base.host.permissions_helper.grants.probe import HelperChannel

GRANT_SERVICES = ("kTCCServiceScreenCapture", "kTCCServiceAccessibility")

DESCRIPTION = (
    "Ask the permissions helper to raise its Screen Recording and Accessibility dialogs, then"
    " record tccd's own verdict as evidence. tccd splits the two: the AUTHREQ_CTX line names"
    " the service and carries preflight=no for a real request, the AUTHREQ_RESULT line carries"
    " the verdict but only a msgID. The run therefore pairs them -- a preflight line can never"
    " pass as evidence of a request. Exit 0 = a verdict matched; 1 = the request ran but no"
    " verdict matched (check the window, or the dialog is still pending); 2 = setup problem"
    " (not macOS, helper unreachable, helper build without request_grants_v1). An Accessibility"
    " request may leave no tccd trace at all; Screen Recording is the service the acceptance run"
    " requires."
)

MSG_ID = re.compile(r"msgID=([0-9.]+)")


def _requested_services(lines: list[str]) -> dict[str, set[str]]:
    services: dict[str, set[str]] = {}
    for line in lines:
        if "preflight=no" not in line:
            continue
        matched = {service for service in GRANT_SERVICES if service in line}
        if not matched:
            continue
        for message_id in MSG_ID.findall(line):
            services.setdefault(message_id, set()).update(matched)
    return services


def decision_evidence(output: str) -> tuple[list[str], set[str]]:
    lines = output.splitlines()
    services = _requested_services(lines)
    verdicts = {
        message_id
        for message_id in services
        if any(f"msgID={message_id}" in line and "AUTHREQ_RESULT" in line for line in lines)
    }
    decided: set[str] = set()
    for message_id in verdicts:
        decided.update(services[message_id])
    rows = [line for line in lines if any(f"msgID={message_id}" in line for message_id in verdicts)]
    return list(dict.fromkeys(rows)), decided


def tccd_log(
    start: datetime,
    window: int,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    local = start.astimezone()
    end = local + timedelta(seconds=window)
    fmt = "%Y-%m-%d %H:%M:%S"
    done = run(
        [
            "log",
            "show",
            "--start",
            local.strftime(fmt),
            "--end",
            end.strftime(fmt),
            "--predicate",
            'process == "tccd"',
            "--style",
            "compact",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout or ""


def main(
    argv: list[str] | None = None,
    *,
    client: HelperChannel | None = None,
    platform: str | None = None,
    log_reader: Callable[[datetime, int], str] = tccd_log,
) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--out", help="evidence directory (default: $MEIVA_HOME/tcc-evidence)")
    parser.add_argument("--window", type=int, default=120, help="seconds after the request")
    parser.add_argument(
        "--require",
        choices=GRANT_SERVICES,
        help="also require a decision line for this service (Screen Recording acceptance)",
    )
    args = parser.parse_args(argv)
    if (platform or sys.platform) != "darwin":
        print("this evidence run needs macOS", file=sys.stderr)
        return 2
    if client is None:
        from services.desktop.permissions_helper.onboarding import helper_client

        client = helper_client()
    try:
        before: dict[str, Any] = dict(client.ping())
    except Exception as exc:
        print(f"the permissions helper did not answer: {exc}", file=sys.stderr)
        return 2
    if before.get("request_grants_v1") is not True:
        print(
            "the running helper predates request_grants_v1; update it before recording evidence",
            file=sys.stderr,
        )
        return 2
    started = datetime.now(UTC)
    requested = dict(client.request_grants())
    after = dict(client.ping())
    lines, decided = decision_evidence(log_reader(started, args.window))
    from base.paths import meiva_home

    out = Path(args.out) if args.out else meiva_home() / "tcc-evidence"
    target = out / f"request-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "started_at": started.isoformat(),
                "helper_pid": before.get("pid"),
                "before": before,
                "requested": requested,
                "after": after,
                "required_service": args.require,
                "decided_services": sorted(decided),
                "tccd_lines": lines,
            },
            indent=2,
        )
    )
    print(f"evidence: {target}")
    for line in lines:
        print(f"  tccd: {line}")
    if args.require and args.require not in decided:
        print(f"tccd recorded no verdict for {args.require}", file=sys.stderr)
        return 1
    if not lines:
        print(
            "no tccd verdict in the window; the dialog may still be pending -- widen --window"
            " and run again",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
