"""What Postgres is told about WAL archiving, in one place.

The settings are launch arguments (`-c`) of the cluster's postmaster, never
`ALTER SYSTEM` and never a line in `postgresql.conf`: the code is the only source
of truth, nothing is written into the data directory (so a base backup carries
no archive setting and a restored instance cannot archive into the live chain by
accident), and turning WAL-G off is "unset the key, restart" with nothing left behind.

`archive_mode` is a postmaster-context setting: it only takes effect when Postgres
is newly launched. A retained postmaster that is merely reloaded keeps its old
launch arguments, so switching archiving on (or off) is an explicit
`ava stop` + `ava start`; the health probe reports a Postgres that disagrees with
the configured expectation.

The command itself holds no secret: the pinned binary, `--config <file>` and
`wal-push %p`. WAL-G reads the credentials from the 0600 file on every call.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from base.cluster.dataplane.walg_binary import walg_path
from services.gateway_side.walg.config import configured_path

ARCHIVE_TIMEOUT_S = 60
"""Force a WAL segment switch at least this often. Design ruling (RPO <= 5 minutes);
a near-idle database then ships a segment every ~60 s (measured max 61.9 s)."""

RPO_OBJECTIVE_S = 300
"""The recovery-point objective: committed data older than this must be in the archive.
The only time threshold of the archive health probe (RPO <= 5 minutes)."""


@dataclass(frozen=True)
class ExpectedArchive:
    """The archive settings a Postgres launched by this code carries."""

    mode: str
    timeout_s: int
    command: str


def _percent_escaped(text: str) -> str:
    """Postgres reads `%p`/`%f` in an archive command and `%%` as a literal percent."""
    return text.replace("%", "%%")


def expected_archive() -> ExpectedArchive | None:
    """The archive settings for the configured WAL-G, or None when WAL-G is off."""
    config_file = configured_path()
    if config_file is None:
        return None
    command = shlex.join(
        [
            _percent_escaped(str(walg_path())),
            "--config",
            _percent_escaped(str(config_file)),
            "wal-push",
            "%p",
        ]
    )
    return ExpectedArchive(mode="on", timeout_s=ARCHIVE_TIMEOUT_S, command=command)


def archive_pg_args() -> list[str]:
    """`postgres` launch arguments for the configured archiving; empty when WAL-G is off."""
    expected = expected_archive()
    if expected is None:
        return []
    return [
        "-c",
        f"archive_mode={expected.mode}",
        "-c",
        f"archive_timeout={expected.timeout_s}s",
        "-c",
        f"archive_command={expected.command}",
    ]
