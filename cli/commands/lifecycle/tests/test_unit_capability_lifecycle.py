"""A pure runner's launcher delivers the installed unit capability."""

from __future__ import annotations

from pathlib import Path

import pytest

from base.cluster import authority
from base.cluster.authority import unit
from base.cluster.authority.tests.unit_capability_support import gateway as gateway
from base.cluster.authority.tests.unit_capability_support import runner_boot as runner_boot
from base.cluster.authority.tests.unit_capability_support import runner_home as runner_home
from cli.commands.data_plane import bringup
from cli.commands.lifecycle.start_generation import _write_generation


def test_a_pure_runners_launcher_delivers_the_installed_capability(
    runner_boot: unit.UnitCapability, runner_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AVA_HOME", str(runner_home))
    monkeypatch.setattr("base.host.env.bootstrap.config_source_is_local", lambda: False)
    assert bringup.db_delivery("runner") == {
        "AVA_DB_URL": runner_boot.dsn,
        authority.GENERATION_ENV: "0",
    }
    with pytest.raises(RuntimeError, match="cannot launch a gateway-class"):
        bringup.db_delivery("gateway")
    # The launch digest binds the capability's non-secret reference.
    assert _write_generation(runner_home) == runner_boot.reference
    unit.unit_capability_path(runner_home).unlink()
    assert _write_generation(runner_home) is None
    with pytest.raises(unit.UnitCapabilityError, match="holds no database capability"):
        bringup.db_delivery("runner")
