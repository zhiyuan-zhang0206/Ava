"""Naming grammar for the off-site logical dump namespace (``ava-logical/``).

One source of truth for the logical-pool file name, shared by the writer
(``services.backup``) and the retention classifier: the writer names every
artifact through this grammar, and the retention planner deletes only names
it can parse here. Strictness is the point -- a name outside the grammar is
never a deletion candidate (fail closed).

The pool is flat: ``ava-logical/<db>-<stamp>[.<kind>].dump.enc`` (the
pre-cutover ciphertext suffix ``.dump.gz.enc`` is inside the grammar). The
optional ``kind`` segment separates the two special artifact classes from
the daily dumps: ``pre-update`` snapshots and ``pitr-activation[-<uuid>]``
snapshots (an activation operation's recovery floor).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo

REMOTE_ROOT = "ava-logical"
"""The off-site logical-dump namespace; a sibling of the PITR prefix."""

DUMP_NAME_RE = re.compile(
    r"^(?P<db>.+)-(?P<ts>\d{8}T\d{6}Z|\d{8}-\d{6})"
    r"(?:\.(?P<kind>pre-update|pitr-activation(?:-[0-9a-f-]{36})?))?\.dump(?:\.gz\.enc|\.enc)?$"
)
TS_FORMAT = "%Y%m%dT%H%M%SZ"  # UTC, offset-bearing by construction
LEGACY_TS_FORMAT = "%Y%m%d-%H%M%S"  # pre-cutover names: wall clock, no offset
PRE_UPDATE_MARKER = "pre-update"
ACTIVATION_MARKER = "pitr-activation"

KIND_DAILY = "daily"
KIND_PRE_UPDATE = "pre-update"
KIND_ACTIVATION = "activation"
KINDS = (KIND_DAILY, KIND_PRE_UPDATE, KIND_ACTIVATION)


@dataclass(frozen=True)
class LogicalDumpName:
    """One managed file name, read through the grammar."""

    db: str
    stamp: str
    kind: str

    def __post_init__(self) -> None:
        if not self.db or self.kind not in KINDS:
            raise ValueError("logical dump name fields are invalid")

    @property
    def legacy(self) -> bool:
        """Whether the stamp predates the offset-bearing UTC format."""
        return not self.stamp.endswith("Z")


def parse_dump_name(name: str) -> LogicalDumpName | None:
    """The grammar's reading of a bare pool file name; None when outside it."""
    match = DUMP_NAME_RE.match(name)
    if match is None:
        return None
    marker = match.group("kind")
    if marker is None:
        kind = KIND_DAILY
    elif marker.startswith(ACTIVATION_MARKER):
        kind = KIND_ACTIVATION
    else:
        kind = KIND_PRE_UPDATE
    return LogicalDumpName(match.group("db"), match.group("ts"), kind)


def relative_name(object_name: str, *, root: str = REMOTE_ROOT) -> str | None:
    """The bare file name of an object directly under ``root``, or None.

    The writer publishes flat names only, so a nested path under the
    namespace is outside the managed grammar.
    """
    prefix = f"{root.rstrip('/')}/"
    if not object_name.startswith(prefix):
        return None
    relative = object_name[len(prefix) :]
    if not relative or "/" in relative:
        return None
    return relative


def stamp_utc(stamp: str, legacy_tz: tzinfo | None) -> datetime:
    """A stamp token as an aware UTC instant.

    A ``Z``-suffixed stamp is UTC by construction. A legacy stamp carries no
    offset, so it is read in ``legacy_tz`` -- the cluster wall clock the
    writer lived in (``.replace(tzinfo=...)``, fold=0, keeps the reading
    deterministic through the DST fall-back hour). Without a timezone the
    legacy reading is refused rather than guessed.
    """
    if stamp.endswith("Z"):
        return datetime.strptime(stamp, TS_FORMAT).replace(tzinfo=UTC)
    if legacy_tz is None:
        raise ValueError("a legacy stamp needs the cluster timezone to be read")
    return datetime.strptime(stamp, LEGACY_TS_FORMAT).replace(tzinfo=legacy_tz).astimezone(UTC)
