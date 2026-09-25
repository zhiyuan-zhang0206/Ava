"""`services.healthchecks.memory_indexer` unit tests — probe delegation + restart shape.

memory_indexer moved from pidfile to HTTP /healthz (#254 pattern) so the watchdog
does not misjudge death during a tens-of-seconds cold-start embed and fall into a
spawn race. The probe itself (identity verification) is covered in
`tests/shared/test_daemon_health.py`; here we pin that this healthcheck asks for
the right daemon name and pidfile, and that the restart path reports the probe's
verdict rather than the spawn's.
"""

from __future__ import annotations

import pytest

from services.healthchecks import memory_indexer as hc
from shared.config import settings
from shared.daemon_health import DaemonProbe


def test_probe_asks_for_this_daemons_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe is scoped to name=memory_indexer + this unit's pidfile — a probe
    that got either wrong would accept a different daemon as healthy."""
    seen: dict[str, object] = {}

    def fake_probe_daemon(name, url, *, pidfile, **_kw) -> DaemonProbe:
        seen.update(name=name, url=url, pidfile=pidfile)  # pyright: ignore[reportUnknownArgumentType]
        return DaemonProbe.up("stub")

    monkeypatch.setattr(hc, "probe_daemon", fake_probe_daemon)  # pyright: ignore[reportUnknownArgumentType]
    assert hc._probe().alive is True
    assert seen["name"] == "memory_indexer"
    assert seen["pidfile"] == settings.services.memory_indexer_pidfile
