"""`wal-g wal-verify integrity timeline --json`: is the archived WAL chain whole.

Only the JSON status is read. The exit code means nothing here: in v3.0.9 the
command exits 0 while it reports a gap (measured), so a check on the exit code
would call a broken chain healthy.

`FAILURE` is the only broken state: a segment between the oldest backup and the
current one is missing and not on its way (`MISSING_LOST`) or the timelines in
storage and the cluster disagree. `WARNING` (`MISSING_UPLOADING`,
`MISSING_DELAYED`) is archiving still in flight, which the archiver probe already
watches, so it is not a failure here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from services.gateway_side.walg.backups import QUERY_TIMEOUT_S
from services.gateway_side.walg.runner import run_walg

_STATUSES = frozenset({"OK", "WARNING", "FAILURE"})


class VerifyOutputError(RuntimeError):
    """`wal-verify --json` printed something this code does not understand."""


@dataclass(frozen=True)
class ChainVerdict:
    integrity: str
    timeline: str

    @property
    def failed(self) -> bool:
        return "FAILURE" in (self.integrity, self.timeline)


def parse_verdict(text: str) -> ChainVerdict:
    """The two check statuses in `text`; an unknown status is an error, never a pass.

    Raises:
        VerifyOutputError: not JSON, a check is missing, or a status is not one of
            OK / WARNING / FAILURE.
    """
    try:
        payload: Any = json.loads(text)
        report = cast(dict[str, Any], payload)
        statuses = (str(report["integrity"]["status"]), str(report["timeline"]["status"]))
    except (ValueError, KeyError, TypeError):
        raise VerifyOutputError(
            "wal-verify did not print the integrity and timeline checks"
        ) from None
    unknown = [status for status in statuses if status not in _STATUSES]
    if unknown:
        raise VerifyOutputError(f"wal-verify reported an unknown status: {unknown[0]!r}")
    return ChainVerdict(integrity=statuses[0], timeline=statuses[1])


def verify_chain() -> ChainVerdict:
    """Run the check against the bucket (it lists every archived segment)."""
    text = run_walg(["wal-verify", "integrity", "timeline", "--json"], timeout_s=QUERY_TIMEOUT_S)
    return parse_verdict(text)
