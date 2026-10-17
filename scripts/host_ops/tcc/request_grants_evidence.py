from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from base.host.permissions_helper.grants.probe import HelperChannel

GRANT_SERVICES = ("kTCCServiceScreenCapture", "kTCCServiceAccessibility")

DESCRIPTION = (
    "Ask the permissions helper to raise its Screen Recording and Accessibility dialogs, then"
    " record tccd's own answer as evidence. Exit 0 = tccd logged a decision for one of the two"
    " grants; 1 = the request ran but no tccd record matched (check the log window); 2 = setup"
    " problem (not macOS, helper unreachable, helper build without request_grants_v1)."
)


def grant_log_lines(output: str) -> list[str]:
    return [
        line for line in output.splitlines() if any(service in line for service in GRANT_SERVICES)
    ]


def tccd_log(
    seconds: int, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
) -> str:
    done = run(
        [
            "log",
            "show",
            "--last",
            f"{seconds}s",
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
    log_reader: Callable[[int], str] = tccd_log,
) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--out")
    parser.add_argument("--window", type=int, default=120)
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
    lines = grant_log_lines(log_reader(args.window))
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
                "tccd_lines": lines,
            },
            indent=2,
        )
    )
    print(f"evidence: {target}")
    for line in lines:
        print(f"  tccd: {line}")
    if not lines:
        print(
            "no tccd decision line matched; the dialog may still be pending -- widen --window",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
