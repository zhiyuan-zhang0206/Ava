"""The deploy window's alert silence: one Grafana silence over every rule, opened by
`cli.fleet_update down` and expired by `up`.

An update stops the whole cluster on purpose, so every rule that watches a service would fire
for a planned outage. Notification policy owns merging and repetition; this is the one place a
planned window is expressed: a silence in the co-located Grafana's Alertmanager whose matcher
covers every alert but the disk ones (`alertname=~".+"`, `metric!="host_disk"`, `attributes_check!="disk_usage"`), carrying an expiry and a comment, created before the
first stop and deleted after `up` succeeds. A silenced rule still evaluates, so a condition that
outlives the window notifies as soon as the silence ends.

`cli.fleet_update` pipes this file's source to `python -` on the gateway host (the same way it
ships its roster program), so the first update that carries this change already has it and the
program needs nothing from the host's checkout but `base.config`. Opening is idempotent (a rerun
of `down` extends the silence it already owns instead of stacking another) and every failure is
reported, never raised: a window without a silence is noisy, not unsafe, so it must not strand
an update.

    python - open --hours 4 --comment "..."   # prints `SILENCE opened ...`
    python - close                            # prints `SILENCE closed N`
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

CREATED_BY = "ava-fleet-update"
_SILENCES = "/api/alertmanager/grafana/api/v2/silences"
_SILENCE = "/api/alertmanager/grafana/api/v2/silence"
# Every alert but a full disk: the disk fills the same during an update, so the host filesystem rules
# (`metric=host_disk`) and the health probe's disk check must still page.
_MATCH_EVERY_ALERT = [
    {"name": "alertname", "value": ".+", "isRegex": True, "isEqual": True},
    {"name": "metric", "value": "host_disk", "isRegex": False, "isEqual": False},
    {"name": "attributes_check", "value": "disk_usage", "isRegex": False, "isEqual": False},
]
_TIMEOUT_S = 15.0
_LIVE_STATES = ("active", "pending")


class GrafanaError(RuntimeError):
    """Grafana could not be reached or refused the call."""


def _call(base: str, credential: str, method: str, path: str, body: object = None) -> Any:
    token = base64.b64encode(f"admin:{credential}".encode()).decode()
    headers = {"Authorization": f"Basic {token}"}
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)  # noqa: S310 — the operator's own Grafana
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310
            raw = response.read()
    except urllib.error.HTTPError as error:
        raise GrafanaError(f"{method} {path}: HTTP {error.code}") from error
    except (OSError, urllib.error.URLError) as error:
        raise GrafanaError(f"{method} {path}: {type(error).__name__}") from error
    return json.loads(raw) if raw else None


def _owned(base: str, credential: str) -> list[dict[str, Any]]:
    """Every live silence this program created."""
    rows = _call(base, credential, "GET", _SILENCES)
    return [
        row
        for row in rows
        if row.get("createdBy") == CREATED_BY and row["status"]["state"] in _LIVE_STATES
    ]


def open_window(base: str, credential: str, *, hours: float, comment: str, now: datetime) -> str:
    """Create (or extend the one already owned) silence over every alert; its id."""
    ends_at = (now + timedelta(hours=hours)).astimezone(UTC).isoformat(timespec="seconds")
    silence: dict[str, Any] = {
        "matchers": _MATCH_EVERY_ALERT,
        "startsAt": now.astimezone(UTC).isoformat(timespec="seconds"),
        "endsAt": ends_at,
        "createdBy": CREATED_BY,
        "comment": comment,
    }
    owned = _owned(base, credential)
    if owned:
        silence["id"] = owned[0]["id"]
        silence["startsAt"] = owned[0]["startsAt"]
    created = _call(base, credential, "POST", _SILENCES, silence)
    return str(created["silenceID"])


def close_window(base: str, credential: str) -> list[str]:
    """Expire every live silence this program created; the ids expired."""
    expired: list[str] = []
    for row in _owned(base, credential):
        _call(base, credential, "DELETE", f"{_SILENCE}/{row['id']}")
        expired.append(str(row["id"]))
    return expired


def _grafana() -> tuple[str, str] | None:
    """The co-located Grafana's base URL and admin credential; None when this home has no credential."""
    from base.config import settings

    secret = settings.alerts.grafana_admin_password
    if secret is None or not secret.get_secret_value():
        return None
    return settings.observability.telemetry_grafana_url.rstrip("/"), secret.get_secret_value()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="fleet_alert_silence")
    verbs = parser.add_subparsers(dest="verb", required=True)
    opened = verbs.add_parser("open")
    opened.add_argument("--hours", type=float, required=True)
    opened.add_argument("--comment", required=True)
    verbs.add_parser("close")
    args = parser.parse_args(argv)
    grafana = _grafana()
    if grafana is None:
        print("SILENCE skipped: no Grafana admin credential is configured on this host")
        return 0
    base, credential = grafana
    try:
        if args.verb == "open":
            now = datetime.now(UTC)
            silence_id = open_window(
                base, credential, hours=args.hours, comment=args.comment, now=now
            )
            until = (now + timedelta(hours=args.hours)).isoformat(timespec="minutes")
            print(f"SILENCE opened id={silence_id} until={until}")
        else:
            print(f"SILENCE closed {len(close_window(base, credential))}")
    except GrafanaError as error:
        print(f"SILENCE failed: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
