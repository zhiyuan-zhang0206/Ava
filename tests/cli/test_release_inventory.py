# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
"""Unit tests for the launchd registration classification channel.

One user namespace carries registrations beyond the unit: machine-level jobs
and the permissions-helper keeper are classified explicitly and recorded, while
unknown ownership keeps refusing the whole inventory.
"""

from __future__ import annotations

import hashlib
import plistlib
from pathlib import Path

import pytest

from cli.commands import _release_inventory as inventory
from shared.runtime_release import ReleaseRejectedError

HOME = Path("/unit")


def _job(directory: Path, label: str, environment: dict[str, str]) -> str:
    path = directory / f"{label}.plist"
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "EnvironmentVariables": environment,
                "ProgramArguments": ["/usr/bin/true"],
            }
        )
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def launch_agents(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    directory = tmp_path / "Library/LaunchAgents"
    directory.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        inventory,
        "read_launchd_definition",
        lambda label: (directory / f"{label}.plist").read_bytes(),
    )
    monkeypatch.setattr(inventory, "read_launchd_labels", lambda _until: frozenset())
    return directory


def _loaded(monkeypatch: pytest.MonkeyPatch, labels: set[str]) -> None:
    monkeypatch.setattr(inventory, "read_launchd_labels", lambda _until: frozenset(labels))


def test_unit_machine_and_keeper_registrations_are_classified(
    launch_agents: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = "com.ava.proof.probe"
    machine = "com.ava.machine.caffeinate"
    keeper = "com.ava.permissions-helper.proof"
    unit_digest = _job(launch_agents, unit, {"AVA_HOME": str(HOME)})
    machine_digest = _job(launch_agents, machine, {"AVA_JOB_SCOPE": "machine"})
    keeper_digest = _job(
        launch_agents,
        keeper,
        {"AVA_PERMISSIONS_HELPER_SOCKET": f"{HOME}/run/permissions-helper.9223.sock"},
    )
    _loaded(monkeypatch, {unit, machine, keeper})

    launchers, excluded = inventory._launchd(HOME)

    assert [(item.name, item.definition_digest) for item in launchers] == [(unit, unit_digest)]
    assert [item.model_dump(mode="json") for item in excluded] == [
        {"label": machine, "definition_digest": machine_digest, "classification": "machine"},
        {"label": keeper, "definition_digest": keeper_digest, "classification": "keeper"},
    ]


def test_unclassified_registration_refuses_the_whole_inventory(launch_agents: Path) -> None:
    _job(launch_agents, "com.ava.legacy.unowned", {})
    with pytest.raises(ReleaseRejectedError, match="unknown or other unit home"):
        inventory._launchd(HOME)


def test_other_home_registration_refuses(launch_agents: Path) -> None:
    _job(launch_agents, "com.ava.other.probe", {"AVA_HOME": "/other"})
    with pytest.raises(ReleaseRejectedError, match="unknown or other unit home"):
        inventory._launchd(HOME)


def test_keeper_outside_this_home_refuses(launch_agents: Path) -> None:
    _job(
        launch_agents,
        "com.ava.permissions-helper.other",
        {"AVA_PERMISSIONS_HELPER_SOCKET": "/other/run/permissions-helper.1.sock"},
    )
    with pytest.raises(ReleaseRejectedError, match="belongs to another home"):
        inventory._launchd(HOME)


def test_keeper_socket_must_be_absolute(launch_agents: Path) -> None:
    _job(
        launch_agents,
        "com.ava.permissions-helper.relative",
        {"AVA_PERMISSIONS_HELPER_SOCKET": "run/permissions-helper.1.sock"},
    )
    with pytest.raises(ReleaseRejectedError, match="socket is malformed"):
        inventory._launchd(HOME)


def test_keeper_socket_must_be_normalized(launch_agents: Path) -> None:
    _job(
        launch_agents,
        "com.ava.permissions-helper.unnormalized",
        {"AVA_PERMISSIONS_HELPER_SOCKET": f"{HOME}/run/../run/permissions-helper.1.sock"},
    )
    with pytest.raises(ReleaseRejectedError, match="socket is malformed"):
        inventory._launchd(HOME)


def test_unknown_scope_value_refuses(launch_agents: Path) -> None:
    _job(launch_agents, "com.ava.scope.unknown", {"AVA_JOB_SCOPE": "unit"})
    with pytest.raises(ReleaseRejectedError, match="unknown job scope"):
        inventory._launchd(HOME)


def test_conflicting_scope_and_home_refuse(launch_agents: Path) -> None:
    _job(
        launch_agents,
        "com.ava.conflict.home",
        {"AVA_JOB_SCOPE": "machine", "AVA_HOME": str(HOME)},
    )
    with pytest.raises(ReleaseRejectedError, match="conflicting ownership declarations"):
        inventory._launchd(HOME)


def test_conflicting_scope_and_keeper_refuse(launch_agents: Path) -> None:
    _job(
        launch_agents,
        "com.ava.conflict.keeper",
        {
            "AVA_JOB_SCOPE": "machine",
            "AVA_PERMISSIONS_HELPER_SOCKET": f"{HOME}/run/permissions-helper.1.sock",
        },
    )
    with pytest.raises(ReleaseRejectedError, match="conflicting ownership declarations"):
        inventory._launchd(HOME)


def test_loaded_job_without_classified_definition_refuses(
    launch_agents: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = "com.ava.proof.probe"
    _job(launch_agents, unit, {"AVA_HOME": str(HOME)})
    _loaded(monkeypatch, {unit, "com.ava.loaded.ghost"})
    with pytest.raises(ReleaseRejectedError, match="no inventoried definition"):
        inventory._launchd(HOME)
