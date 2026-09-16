"""The host's service-driver switch, read in exactly one place.

`services.root_driver_enabled` decides who owns this host's service tree: the
ava-root supervisor (units) or the session path (`ava start`'s named
sessions). The CLI driver and the health probes both ask, and their answers
must not drift — two hand-copied readers of one switch is how a host ends up
half-switched (task #3598, folding the copies from `cli.commands._root_driver`
and `services.healthchecks.frontend`).

The read rule (shared with the switch family's `helper_spawn_enabled`): a
configuration failure reads as OFF — the session path — because the switch is
a deliberate per-host commitment, and an unreadable config is not evidence of
one.
"""

from __future__ import annotations


def root_drive_enabled() -> bool:
    """Whether this host routes its services through the ava-root supervisor.

    Read at call time (settings, env, monkeypatched tests — never cached).
    This is the unit's management mode read from its own definition, never a
    per-check sniff of "is a root there".
    """
    try:
        from shared.config import settings

        return bool(settings.services.root_driver_enabled)
    except Exception:
        return False
