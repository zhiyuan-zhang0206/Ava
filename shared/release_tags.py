"""Dated release-tag parsing + selection, shared by `scripts/release_cut.py` and
the update track (`AVA_TRACK_MODE=releases`).

A release tag is `v<major>.<minor>.<patch>-<YYYYMMDD>[HHMM]` — the date suffix
makes every tag self-dating, so a day with no release simply has no tag. The
selection rule: highest version wins; on equal versions the later HHMM wins (a
tag without HHMM is earliest on its day). release_cut mints these tags; the
update path resolves "the newest release" to pin a rollout to it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

# Dated three-segment tag: v<major>.<minor>.<patch>-<YYYYMMDD>[-HHMM]
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)-(\d{8})-?(\d{4})?$")


@dataclass(frozen=True)
class ReleaseTag:
    """One parsed dated release tag."""

    name: str
    version: tuple[int, int, int]
    day: date
    hhmm: str | None


def parse_release_tag(raw: str) -> ReleaseTag | None:
    """Parse one raw tag name; None when it is not a dated release tag."""
    m = _TAG.match(raw.strip())
    if m is None:
        return None
    return ReleaseTag(
        name=raw.strip(),
        version=(int(m[1]), int(m[2]), int(m[3])),
        day=date(int(m[4][:4]), int(m[4][4:6]), int(m[4][6:8])),
        hhmm=m[5] if m[5] else None,
    )


def pick_latest_tag(tags: Iterable[str]) -> ReleaseTag | None:
    """The highest dated release tag in `tags` (None when none parse).

    Same-version tags (same-day multi-cut) resolve to the later HHMM; a tag
    without HHMM is earliest on its day.
    """
    latest: ReleaseTag | None = None
    for raw in tags:
        tag = parse_release_tag(raw)
        if tag is None:
            continue
        if latest is None or tag.version > latest.version:
            latest = tag
        elif tag.version == latest.version:
            cur_key = tag.hhmm or ""
            prev_key = latest.hhmm or ""
            if cur_key > prev_key:
                latest = tag
    return latest
