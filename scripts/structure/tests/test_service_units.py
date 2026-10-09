"""Placement units under `services/`: a service inside a group, or a package beside the groups."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.structure import placement, service_units
from scripts.structure.tests.patch_repo import make_repo


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


@pytest.mark.parametrize(
    ("rel", "text", "target"),
    [
        ("daemon.py", "from ..dispatch import runner as worker", "services.wake.dispatch"),
        ("__init__.py", "from ..dispatch import runner", "services.wake.dispatch"),
        ("daemon.py", "from ... import redis_bridge as bridge", "services.redis_bridge"),
        ("daemon.py", "from ...redis_bridge import relay", "services.redis_bridge"),
        ("daemon.py", "from ... import pidfile", "services.pidfile"),
        ("daemon.py", "from services.wake import dispatch", "services.wake.dispatch"),
    ],
)
def test_production_import_edges_resolve_cross_unit_module_members(
    tmp_path: Path, rel: str, text: str, target: str
) -> None:
    root = make_repo(
        tmp_path,
        {
            f"services/wake/heartbeat/{rel}": text,
            "services/wake/dispatch/runner.py": "",
            "services/redis_bridge/relay.py": "",
            "services/pidfile.py": "",
        },
    )
    graph = placement.unit_graph(root)
    source = "services.wake.heartbeat"
    assert graph.empirical[source, target] == 1
    assert graph.empirical[target, source] == 0
    assert graph.can_import(source, target)
    assert not graph.can_import(target, source)


def test_an_invalid_relative_import_does_not_create_a_root_unit_edge(tmp_path: Path) -> None:
    root = make_repo(
        tmp_path,
        {"services/wake/heartbeat/daemon.py": "from ....base.net import retry"},
    )
    graph = placement.unit_graph(root)
    assert graph.empirical["services.wake.heartbeat", "base"] == 0
    assert not graph.can_import("services.wake.heartbeat", "base")
