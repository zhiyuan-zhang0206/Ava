"""Naming grammar of the daily logical dump pool.

One source of truth for a managed dump's file name, shared by the local
due-ness and prune logic (`services.backup.dump`), the scheduled commit
(`services.backup.scheduler.worker`) and the off-site namespace
(`services.backup.artifact.offsite`). The pool is flat:
``<db>-<UTC stamp>.dump.enc`` (the ciphertext suffix ``.dump.gz.enc`` and a
bare ``.dump`` stay inside the grammar). A file outside it is never counted,
pruned or published as a managed dump: a hand-made dump parked beside them is
left alone.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

REMOTE_ROOT = "ava-logical"
"""The off-site logical-dump namespace."""

DUMP_NAME_RE = re.compile(r"^(?P<db>.+)-(?P<ts>\d{8}T\d{6}Z)\.dump(?:\.gz\.enc|\.enc)?$")
TS_FORMAT = "%Y%m%dT%H%M%SZ"  # UTC, offset-bearing by construction


def stamp_utc(stamp: str) -> datetime:
    """A name's stamp token as an aware UTC instant.

    The token is UTC by construction (its ``Z`` is part of the grammar), so
    ordering stamps orders real instants whatever the host's or cluster's clock.
    """
    return datetime.strptime(stamp, TS_FORMAT).replace(tzinfo=UTC)
