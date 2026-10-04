"""Placement units under `services/`: a service inside a group, or a package beside the groups."""

from __future__ import annotations

from pathlib import Path

from scripts.structure import placement, service_units


def test_module_in_a_group_belongs_to_its_service_package() -> None:
    assert service_units.unit_of(["services", "wake", "heartbeat", "daemon"]) == (
        "services.wake.heartbeat"
    )
    assert placement.unit_of("services.backup.dump") == "services.backup.dump"


def test_module_beside_the_groups_belongs_to_its_package() -> None:
    assert placement.unit_of("services.redis_bridge.relay") == "services.redis_bridge"
    assert placement.unit_of("services.pidfile") == "services.pidfile"


def test_units_are_service_packages_not_groups(tmp_path: Path) -> None:
    services = tmp_path / "services"
    for path in (
        "wake/heartbeat/daemon.py",
        "wake/tests/test_x.py",
        "wake/docs/wake.md",
        "backup/dump.py",
        "redis_bridge/relay.py",
        "docs/services.md",
        "pidfile.py",
    ):
        (services / path).parent.mkdir(parents=True, exist_ok=True)
        (services / path).write_text("")
    assert service_units.build(services) == {
        "services.wake.heartbeat",
        "services.backup.dump",
        "services.redis_bridge",
        "services.pidfile",
    }
