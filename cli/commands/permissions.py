from __future__ import annotations

import argparse
import json
import sys

from base.host.permissions_helper.grants import checklist
from base.host.permissions_helper.grants.gate import GateReport, GateState
from services.desktop.permissions_helper.onboarding import GateService


def run(
    args: argparse.Namespace,
    service: GateService | None = None,
    *,
    interactive: bool | None = None,
) -> int:
    gate = service or GateService()
    human_present = (
        interactive if interactive is not None else sys.stdin.isatty() and sys.stdout.isatty()
    )
    verb = args.permissions_cmd
    if verb == "status":
        return _report(gate.check(), as_json=args.json)
    if verb == "onboard":
        return _report(
            gate.onboard(
                interactive=human_present,
                timeout_s=args.timeout,
                echo=lambda line: print(line, file=sys.stderr),
            ),
            as_json=args.json,
        )
    if verb == "defer":
        gate.defer()
        print(
            "Recorded. Nothing is granted by this: the host stays in its limited state and"
            " the next start asks again."
        )
        return 0
    if verb == "restart-helper":
        print(gate.restart_helper())
        return 0
    raise SystemExit(f"unknown permissions verb {verb!r}")


def _report(report: GateReport, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(checklist.payload(report), indent=2))
    else:
        print(checklist.render_text(report))
    return 0 if report.complete or report.state is GateState.NOT_APPLICABLE else 1
