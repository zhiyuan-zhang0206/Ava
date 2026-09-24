# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
"""Unit tests for the launchd registration classification channel.

One user namespace carries registrations beyond the unit: machine-level jobs
and the permissions-helper keeper are classified explicitly and recorded, while
unknown ownership keeps refusing the whole inventory.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
from pathlib import Path

import pytest

from cli.commands import _release_inventory as inventory
from shared.managed_writer_observation import (
    ExcludedRegistration,
    ExpectedLauncher,
    ExpectedUnitWriters,
)
from shared.runtime_publication_input import PreparationReceipt, _receipt_expected
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


def test_prepared_receipt_body_satisfies_the_publication_consumer() -> None:
    """The sealed body must parse as the shared consumer model (task #4096 pin).

    collect_inventory gained excluded_registrations after PreparationReceipt
    was written; a producer key without the consumer member rejects the whole
    receipt (cold-offline caught the drift on CI only). Assembly and
    consumption stay pinned together here.
    """
    expected = ExpectedUnitWriters(
        machine="proof",
        home=str(HOME),
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        processes=(),
        sessions=(),
        launchers=(
            ExpectedLauncher(
                kind="launchd", name="com.ava.proof.probe", definition_digest="c" * 64
            ),
        ),
    )
    excluded = (
        ExcludedRegistration(
            label="com.ava.machine.caffeinate",
            definition_digest="d" * 64,
            classification="machine",
        ),
    )
    body = inventory._receipt_body(
        expected, excluded, [{"session": "ava-ops", "requires_db": True, "gate": None}]
    )
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()

    receipt = PreparationReceipt.model_validate_json(encoded)
    assert receipt.excluded_registrations == excluded
    assert _receipt_expected(encoded) == expected


def test_foreign_files_without_a_readable_label_stay_outside_the_namespace(
    launch_agents: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Third-party plists (e.g. keystone stubs) never break the inventory."""
    unit = "com.ava.proof.probe"
    _job(launch_agents, unit, {"AVA_HOME": str(HOME)})
    _loaded(monkeypatch, {unit})
    (launch_agents / "com.google.keystone.agent.plist").write_bytes(plistlib.dumps({}))
    (launch_agents / "com.google.keystone.xpcservice.plist").write_bytes(plistlib.dumps({}))
    (launch_agents / "com.other.array.plist").write_bytes(plistlib.dumps(["not", "a", "dict"]))
    (launch_agents / "com.other.text.plist").write_bytes(plistlib.dumps("flat string"))

    launchers, excluded = inventory._launchd(HOME)

    assert [item.name for item in launchers] == [unit]
    assert excluded == ()


def test_ava_named_file_without_a_readable_label_refuses(launch_agents: Path) -> None:
    """A com.ava.*-named file never skips silently, whatever its shape."""
    broken = launch_agents / "com.ava.unlabelled.plist"
    broken.write_bytes(plistlib.dumps({}))
    with pytest.raises(ReleaseRejectedError, match="has no label"):
        inventory._launchd(HOME)
    broken.unlink()
    (launch_agents / "com.ava.array.plist").write_bytes(plistlib.dumps(["not", "a", "dict"]))
    with pytest.raises(ReleaseRejectedError, match="has no label"):
        inventory._launchd(HOME)


def test_neighboring_unit_and_keeper_are_explicit_exclusions(
    launch_agents: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = launch_agents.parents[1] / "own-unit"
    home.mkdir()
    other = launch_agents.parents[1] / "other-unit"
    other.mkdir()
    (other / "machine_name").write_text("other-machine\n")
    (other / "run").mkdir()
    own = "com.ava.this.probe"
    neighbor = "com.ava.other.probe"
    keeper = "com.ava.permissions-helper.other"
    own_digest = _job(launch_agents, own, {"AVA_HOME": str(home)})
    neighbor_digest = _job(launch_agents, neighbor, {"AVA_HOME": str(other)})
    keeper_digest = _job(
        launch_agents,
        keeper,
        {"AVA_PERMISSIONS_HELPER_SOCKET": f"{other}/run/permissions-helper.1234.sock"},
    )
    _loaded(monkeypatch, {own, neighbor, keeper})
    launchers, excluded = inventory._launchd(home)
    assert launchers == (ExpectedLauncher(kind="launchd", name=own, definition_digest=own_digest),)
    assert [(item.label, item.definition_digest, item.classification) for item in excluded] == [
        (neighbor, neighbor_digest, "other-unit"),
        (keeper, keeper_digest, "other-unit"),
    ]
    # Consumer round-trip keeps the exclusion proof, rather than dropping it.
    assert ExcludedRegistration.model_validate_json(excluded[0].model_dump_json()) == excluded[0]


@pytest.mark.parametrize("shape", ["alias", "uninstalled", "relative", "ancestor", "descendant"])
def test_other_unit_exclusion_requires_independent_canonical_installed_home(
    launch_agents: Path,
    shape: str,
) -> None:
    root = launch_agents.parents[1]
    own = root / "own"
    own.mkdir()
    foreign = root / "other"
    foreign.mkdir()
    (foreign / "machine_name").write_text("other-machine\n")
    value = str(foreign)
    if shape == "alias":
        alias = root / "alias"
        alias.symlink_to(foreign, target_is_directory=True)
        value = str(alias)
    elif shape == "uninstalled":
        (foreign / "machine_name").unlink()
    elif shape == "relative":
        value = "other"
    elif shape == "ancestor":
        (root / "machine_name").write_text("ancestor\n")
        value = str(root)
    elif shape == "descendant":
        nested = own / "child"
        nested.mkdir()
        (nested / "machine_name").write_text("descendant\n")
        value = str(nested)
    _job(launch_agents, "com.ava.other.probe", {"AVA_HOME": value})
    with pytest.raises(ReleaseRejectedError, match="unknown or other unit home"):
        inventory._launchd(own)


@pytest.mark.parametrize("relationship", ["same", "ancestor", "descendant"])
def test_case_alias_cannot_exclude_this_units_own_launcher(
    launch_agents: Path, relationship: str
) -> None:
    root = launch_agents.parents[1] / "CaseSensitiveName"
    root.mkdir()
    home = root / "OwnUnit"
    home.mkdir()
    (root / "machine_name").write_text("ancestor\n")
    (home / "machine_name").write_text("own\n")
    nested = home / "Nested"
    nested.mkdir()
    (nested / "machine_name").write_text("nested\n")
    aliases = {
        "same": root / "ownunit",
        "ancestor": root.with_name("casesensitivename"),
        "descendant": root / "ownunit" / "Nested",
    }
    alias = aliases[relationship]
    if not alias.exists():
        pytest.skip("filesystem distinguishes letter case")
    _job(launch_agents, "com.ava.alias.probe", {"AVA_HOME": str(alias)})
    with pytest.raises(ReleaseRejectedError, match="unknown or other unit home"):
        inventory._launchd(home)


def test_other_keeper_cannot_reach_this_home_through_an_aliased_run_directory(
    launch_agents: Path,
) -> None:
    root = launch_agents.parents[1]
    own, other = root / "own", root / "other"
    own.mkdir()
    other.mkdir()
    (own / "run").mkdir()
    (other / "machine_name").write_text("other\n")
    (other / "run").symlink_to(own / "run", target_is_directory=True)
    _job(
        launch_agents,
        "com.ava.other.keeper",
        {
            "AVA_PERMISSIONS_HELPER_SOCKET": f"{other}/run/permissions-helper.123.sock",
        },
    )
    with pytest.raises(ReleaseRejectedError, match="belongs to another home"):
        inventory._launchd(own)
