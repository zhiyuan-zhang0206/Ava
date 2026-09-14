"""Update-survival roster: units a rolling update must never stop first.

G6a(2) ruling (2026-09-14): during an update window the fleet entrance and the
observation surface must keep serving, so "do not black-hole the entrance" is
an explicit update-survival class rather than a structural accident:

- the update orchestration starts a replacement FIRST and waits for its
  readiness criterion before stopping the old instance;
- a stop-first order is forbidden for these units;
- an explicit stop/drain (`ava stop` / pause semantics) is NOT exempt — that
  is the operator deliberately taking everything down.

The roster itself is deployment data: it is filled at wiring time (W1.3) with
the named unit set, and K2 manifests stay closed (no new field). This module is
the code-side skeleton — the predicate the update orchestration must consult
before stopping any unit, and the constant the wiring slice fills.
"""

from __future__ import annotations

UPDATE_SURVIVAL_UNIT_IDS: frozenset[str] = frozenset()
"""Units that must survive update windows (never stop-first).

Empty until the wiring slice names the roster (per G6a(2): the fleet entrance
plus the observation group). The predicate below is the hook; the set is the
single source it reads.
"""


def is_update_survival_unit(unit_id: str) -> bool:
    """Whether `unit_id` requires the start-new-ready-then-stop-old order."""
    return unit_id in UPDATE_SURVIVAL_UNIT_IDS
