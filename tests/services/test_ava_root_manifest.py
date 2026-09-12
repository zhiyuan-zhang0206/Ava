"""services.ava_root.manifest: K2 manifest validation and the registry tree.

The K2 field set is closed and validated fail-fast: unknown fields, malformed
values, duplicate ids, unknown attachments, and attach cycles are all errors.
The registry is the single source of the tree's shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.ava_root.manifest import (
    ROOT_ID,
    ManifestError,
    RestartPolicy,
    UnitManifest,
    UnitRegistry,
    UnknownUnitError,
    load_manifests,
)


def _manifest(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"id": "unit-a", "exec": ["/bin/true"], "restart": "always"}
    base.update(overrides)
    return base


def _unit(unit_id: str, *, attach: str = ROOT_ID, restart: str = "always") -> UnitManifest:
    return UnitManifest(
        id=unit_id, exec=("/bin/true",), restart=RestartPolicy(restart), attach=attach
    )


def test_minimal_manifest_defaults_attach_to_root() -> None:
    manifest = UnitManifest.from_mapping(_manifest(), origin="t")
    assert manifest.id == "unit-a"
    assert manifest.exec == ("/bin/true",)
    assert manifest.restart is RestartPolicy.ALWAYS
    assert manifest.attach == ROOT_ID


def test_full_manifest_parses() -> None:
    manifest = UnitManifest.from_mapping(
        _manifest(attach="unit-b", restart="on-failure"), origin="t"
    )
    assert manifest.attach == "unit-b"
    assert manifest.restart is RestartPolicy.ON_FAILURE


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ManifestError, match="unknown manifest field"):
        UnitManifest.from_mapping(_manifest(depends_on=["x"]), origin="t")


@pytest.mark.parametrize("field", ["id", "exec", "restart"])
def test_missing_required_field_is_rejected(field: str) -> None:
    raw = _manifest()
    del raw[field]
    with pytest.raises(ManifestError, match="missing required field"):
        UnitManifest.from_mapping(raw, origin="t")


@pytest.mark.parametrize("bad_id", ["", "Upper", "with space", "has/slash", "-lead"])
def test_invalid_id_is_rejected(bad_id: str) -> None:
    with pytest.raises(ManifestError, match="id"):
        UnitManifest.from_mapping(_manifest(id=bad_id), origin="t")


def test_root_id_is_reserved() -> None:
    with pytest.raises(ManifestError, match="reserved"):
        UnitManifest.from_mapping(_manifest(id="root"), origin="t")


@pytest.mark.parametrize(
    "bad_exec",
    [
        [],
        "not-a-list",
        [""],
        ["ok", ""],
        ["ok", 3],
    ],
)
def test_invalid_exec_is_rejected(bad_exec: object) -> None:
    with pytest.raises(ManifestError, match="exec"):
        UnitManifest.from_mapping(_manifest(exec=bad_exec), origin="t")


def test_invalid_restart_value_is_rejected() -> None:
    with pytest.raises(ManifestError, match="restart"):
        UnitManifest.from_mapping(_manifest(restart="sometimes"), origin="t")


@pytest.mark.parametrize("bad_attach", [3, "", "Bad Id"])
def test_invalid_attach_is_rejected(bad_attach: object) -> None:
    with pytest.raises(ManifestError, match="attach"):
        UnitManifest.from_mapping(_manifest(attach=bad_attach), origin="t")


def test_error_carries_origin() -> None:
    with pytest.raises(ManifestError, match=r"myfile: units\[0\]"):
        UnitManifest.from_mapping(_manifest(restart="bogus"), origin="myfile: units[0]")


class TestRegistry:
    def test_tree_shape_from_manifests(self) -> None:
        registry = UnitRegistry(
            [
                _unit("alpha"),
                _unit("alpha-child", attach="alpha"),
                _unit("alpha-child-two", attach="alpha"),
                _unit("beta"),
            ]
        )
        assert [m.id for m in registry.units] == [
            "alpha",
            "alpha-child",
            "alpha-child-two",
            "beta",
        ]
        assert registry.children_of(ROOT_ID) == ("alpha", "beta")
        assert registry.children_of("alpha") == ("alpha-child", "alpha-child-two")
        assert registry.children_of("beta") == ()

    def test_subtree_is_parents_before_children(self) -> None:
        registry = UnitRegistry([_unit("alpha"), _unit("child", attach="alpha")])
        assert registry.subtree("alpha") == ("alpha", "child")
        assert registry.subtree("child") == ("child",)

    def test_stop_order_is_children_before_parents(self) -> None:
        registry = UnitRegistry([_unit("alpha"), _unit("child", attach="alpha")])
        assert registry.stop_order("alpha") == ("child", "alpha")
        assert registry.all_stop_order() == ("child", "alpha")

    def test_duplicate_id_is_rejected(self) -> None:
        with pytest.raises(ManifestError, match="duplicate unit id"):
            UnitRegistry([_unit("alpha"), _unit("alpha")])

    def test_unknown_attachment_is_rejected(self) -> None:
        with pytest.raises(ManifestError, match="unknown unit"):
            UnitRegistry([_unit("alpha", attach="ghost")])

    def test_attach_cycle_is_rejected(self) -> None:
        with pytest.raises(ManifestError, match="cycle"):
            UnitRegistry([_unit("a", attach="b"), _unit("b", attach="a")])

    def test_self_attach_is_rejected(self) -> None:
        with pytest.raises(ManifestError, match="cycle"):
            UnitRegistry([_unit("a", attach="a")])

    def test_get_returns_manifest(self) -> None:
        registry = UnitRegistry([_unit("alpha")])
        assert registry.get("alpha").id == "alpha"

    def test_get_unknown_raises(self) -> None:
        registry = UnitRegistry([_unit("alpha")])
        with pytest.raises(UnknownUnitError):
            registry.get("ghost")
        with pytest.raises(UnknownUnitError):
            registry.children_of("ghost")


def test_load_manifests_reads_a_file(tmp_path: Path) -> None:
    path = tmp_path / "units.json"
    path.write_text(
        json.dumps(
            {
                "units": [
                    {"id": "alpha", "exec": ["/bin/true"], "restart": "never"},
                    {"id": "beta", "exec": ["/bin/true"], "restart": "always", "attach": "alpha"},
                ]
            }
        ),
        encoding="utf-8",
    )
    registry = load_manifests(path)
    assert registry.subtree("alpha") == ("alpha", "beta")


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("not json at all", "not valid JSON"),
        ("[1, 2]", "top level must be an object"),
        ('{"units": [], "extra": 1}', "unknown top-level field"),
        ('{"units": "nope"}', "'units' must be a list"),
        ('{"units": [42]}', "must be an object"),
        ('{"units": [{"id": "a", "exec": ["/bin/true"], "restart": "x"}]}', "restart"),
    ],
)
def test_load_manifests_rejects_bad_documents(tmp_path: Path, content: str, match: str) -> None:
    path = tmp_path / "units.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ManifestError, match=match):
        load_manifests(path)


def test_load_manifests_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="cannot read manifest file"):
        load_manifests(tmp_path / "absent.json")
