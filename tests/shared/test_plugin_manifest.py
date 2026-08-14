"""`shared.plugin_manifest` unit tests — manifest validation, range algebra,
pyproject mirror check (the S1–S2 contract layer, task #1244).

Spec: `conventions/plugin-spec-v2.md`.
"""

from pathlib import Path
from typing import Any, cast

import pytest

from shared import plugin_manifest as pm
from shared.plugin_manifest import ManifestError


def _manifest(**overrides: object) -> pm.PluginManifest:
    data: dict[str, object] = {
        "apiVersion": 2,
        "name": "acme",
        "version": "1.0.0",
        "engines": {"ava": ">=0.1.0"},
    }
    data.update(overrides)
    return pm._validate(cast(dict[str, Any], data))


def _deps(python_packages: dict[str, str] | None = None) -> pm.PluginManifest:
    return _manifest(
        dependencies={"pythonPackages": [f"{n}{r}" for n, r in (python_packages or {}).items()]}
    )


# ── ranges ─────────────────────────────────────────────────────────────


def test_range_allows() -> None:
    assert pm.range_allows(">=0.1.0", "0.1.5") is True
    assert pm.range_allows(">=0.1.0,<1", "0.1.5") is True
    assert pm.range_allows(">=0.1.38,<1", "0.1.5") is False
    assert pm.range_allows(">=1,<2", "2.0.0") is False
    assert pm.range_allows("==1.28.1", "1.28.1") is True
    assert pm.range_allows(">1.27,<=2", "1.27.0") is False
    assert pm.range_allows(">1.27,<=2", "2.0.0") is True


def test_parse_range_rejects_unsupported() -> None:
    for bad in ("~=1.0", "!=1.0", ">=1.0.*", "", "^1.0", ">=one"):
        with pytest.raises(ManifestError):
            pm.parse_range(bad)


def test_parse_range_bare_version_means_eq() -> None:
    clauses = pm.parse_range("1.28.1")
    assert len(clauses) == 1
    assert clauses[0].op == "=="
    assert pm.range_allows("1.28.1", "1.28.1") is True
    assert pm.range_allows("1.28.1", "1.28.2") is False


def test_range_allows_bad_version_raises() -> None:
    with pytest.raises(ManifestError):
        pm.range_allows(">=1", "not-a-version")


# ── validation ─────────────────────────────────────────────────────────


def test_minimal_manifest_valid() -> None:
    m = _manifest()
    assert m.name == "acme"
    assert m.version == "1.0.0"
    assert m.engines == {"ava": ">=0.1.0"}
    assert m.dependencies.plugins == {}
    assert m.dependencies.python_packages == {}
    assert m.dependencies.host_capabilities == {}
    assert m.lifecycle.activation == "immediate"
    assert m.lifecycle.dispose == "effect-registry"


def test_validation_reports_every_problem() -> None:
    with pytest.raises(ManifestError) as exc:
        pm._validate(
            {
                "apiVersion": 3,
                "name": "BadName",
                "version": "not-semver",
                "engines": {"ava": ">=0.1.0", "node": ">=1"},
                "bogus": 1,
            }
        )
    text = str(exc.value)
    assert "apiVersion" in text
    assert "name must match" in text
    assert "version must be semver" in text
    assert "unknown host 'node'" in text
    assert "unknown field 'bogus'" in text


def test_unknown_top_level_field_fails() -> None:
    with pytest.raises(ManifestError):
        _manifest(bogus=1)


def test_engines_required() -> None:
    with pytest.raises(ManifestError):
        pm._validate({"apiVersion": 2, "name": "acme", "version": "1.0.0"})


def test_bad_engine_range_fails() -> None:
    with pytest.raises(ManifestError):
        _manifest(engines={"ava": ">=1.*"})


def test_hook_points_validated() -> None:
    _manifest(contributions={"hooks": ["after_init", "before_exec"]})
    with pytest.raises(ManifestError):
        _manifest(contributions={"hooks": ["before_init"]})


def test_unknown_contribution_surface_fails() -> None:
    with pytest.raises(ManifestError):
        _manifest(contributions={"menus": ["x"]})


def test_config_contribution_shape() -> None:
    m = _manifest(
        contributions={"config": {"schema": "default_config.py", "perAgentFields": ["marker"]}}
    )
    assert m.contributions["config"] == {
        "schema": "default_config.py",
        "perAgentFields": ["marker"],
    }
    with pytest.raises(ManifestError):
        _manifest(contributions={"config": {"bogus": 1}})
    with pytest.raises(ManifestError):
        _manifest(contributions={"config": {"schema": 1}})
    with pytest.raises(ManifestError):
        _manifest(contributions={"config": "not-a-dict"})


def test_host_capability_enums() -> None:
    m = _manifest(
        dependencies={"hostCapabilities": {"db": "rw", "network": "local", "display": "required"}}
    )
    assert m.dependencies.host_capabilities["db"] == "rw"
    with pytest.raises(ManifestError):
        _manifest(dependencies={"hostCapabilities": {"db": "admin"}})
    with pytest.raises(ManifestError):
        _manifest(dependencies={"hostCapabilities": {"gpu": "required"}})


def test_lifecycle_only_immediate_activation() -> None:
    with pytest.raises(ManifestError):
        _manifest(lifecycle={"activation": "lazy"})
    with pytest.raises(ManifestError):
        _manifest(lifecycle={"dispose": "manual"})
    m = _manifest(lifecycle={"entry": "plugin.py", "dispose": "none"})
    assert m.lifecycle.entry == "plugin.py"
    assert m.lifecycle.dispose == "none"


# ── pythonPackages hard enforcement (user ruling 2026-08-13) ──────────


def test_python_packages_unbounded_declaration_fails() -> None:
    """The #1198 shape must fail at validation, not at install."""
    with pytest.raises(ManifestError, match="no upper bound"):
        _deps({"mcp": ">=1.27"})
    with pytest.raises(ManifestError, match="no upper bound"):
        _deps({"mcp": ">1.27"})


def test_python_packages_bounded_forms_pass() -> None:
    _deps({"mcp": ">=1.27,<2"})
    _deps({"mcp": "<=2"})
    _deps({"mcp": "==1.28.1"})
    _deps({"mcp": ">=1.27,<=1.30"})


def test_python_packages_missing_range_fails() -> None:
    with pytest.raises(ManifestError, match="has no range"):
        _deps({"mcp": ""})


def test_python_packages_duplicate_fails() -> None:
    with pytest.raises(ManifestError, match="duplicate"):
        pm._validate(
            {
                "apiVersion": 2,
                "name": "acme",
                "version": "1.0.0",
                "engines": {"ava": ">=0.1.0"},
                "dependencies": {"pythonPackages": ["mcp>=1,<2", "mcp>=1,<1.5"]},
            }
        )


def test_plugin_dependencies_range_validated() -> None:
    m = _manifest(dependencies={"plugins": {"other": ">=1.0,<2"}})
    assert m.dependencies.plugins == {"other": ">=1.0,<2"}
    with pytest.raises(ManifestError):
        _manifest(dependencies={"plugins": {"other": ">=1.*"}})


# ── engines check ──────────────────────────────────────────────────────


def test_check_host_engine() -> None:
    m = _manifest(engines={"ava": ">=0.1.38"})
    assert pm.check_host_engine(m, "0.1.38") == []
    assert pm.check_host_engine(m, "0.2.0") == []
    errors = pm.check_host_engine(m, "0.1.5")
    assert len(errors) == 1
    assert "requires Ava >=0.1.38" in errors[0]


def test_host_version_from_repo(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "ava"\nversion = "0.1.5"\n', encoding="utf-8"
    )
    assert pm.host_version_from_repo(tmp_path) == "0.1.5"
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "ava"\n', encoding="utf-8")
    with pytest.raises(ManifestError):
        pm.host_version_from_repo(tmp_path)


# ── pyproject mirror ───────────────────────────────────────────────────


def _mirror(declared: str | None, py: str | None) -> list[str]:
    m = _deps({"x": declared} if declared is not None else None)
    specs = {"x": py} if py is not None else {}
    return pm.check_python_packages(m, specs)


def test_mirror_ok() -> None:
    assert _mirror(">=1.27,<2", ">=1.27,<2.0.0") == []
    assert _mirror(">=1.27,<2", ">=1.30,<2.0") == []
    assert _mirror(">=1.27,<2", "==1.28.1") == []
    assert _mirror(">=1.27,<2", "==1.28.*") == []
    assert _mirror(">=1.27,<2", "~=1.27") == []
    assert _mirror(">=1.27,<2", ">1.27,<2") == []
    assert _mirror("<=1.28", "<1.28") == []


def test_mirror_unbounded_pyproject_is_1198_shape() -> None:
    errors = _mirror(">=1.27,<2", ">=1.27.0")
    assert len(errors) == 1
    assert "#1198" in errors[0]


def test_mirror_reaches_below() -> None:
    errors = _mirror(">=1.27,<2", ">=1.20,<2")
    assert len(errors) == 1
    assert "reaches below" in errors[0]


def test_mirror_reaches_above() -> None:
    errors = _mirror(">=1.27,<2", ">=1.27,<3")
    assert len(errors) == 1
    assert "reaches above" in errors[0]


def test_mirror_exclusive_boundary() -> None:
    # py includes the boundary version the declared range excludes
    assert _mirror(">1.27,<2", ">=1.27,<2") != []
    assert _mirror("<1.28", "<=1.28") != []
    # py stricter at the same boundary is fine
    assert _mirror(">=1.27,<2", ">1.27,<2") == []


def test_mirror_missing_sides() -> None:
    m = _deps({"x": ">=1,<2"})
    assert pm.check_python_packages(m, {}) != []  # declared but absent from pyproject
    m2 = _deps(None)
    errors = pm.check_python_packages(m2, {"y": ">=1,<2"})
    assert len(errors) == 1  # pyproject declares, manifest does not
    assert pm.check_python_packages(m2, {}) == []


def test_mirror_py_lower_but_declared_unbounded_below() -> None:
    errors = _mirror("<2", ">=1.27,<2")
    assert len(errors) == 1
    assert "constrains the lower end" in errors[0]


# ── pyproject dependency extraction ────────────────────────────────────


def _write_pyproject(tmp_path: Path, deps: list[str]) -> Path:
    body = "\n".join(f'"{d}",' for d in deps)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "x"\nversion = "0.0.0"\ndependencies = [\n{body}\n]\n',
        encoding="utf-8",
    )
    return tmp_path


def test_pyproject_dependency_specs(tmp_path: Path) -> None:
    specs = pm.pyproject_dependency_specs(
        _write_pyproject(
            tmp_path,
            [
                "mcp>=1.27,<2.0.0",
                "requests[security]~=2.32",
                "pinned==1.2.3",
                "wild==1.28.*",
                "bare",
            ],
        )
    )
    assert specs == {
        "mcp": ">=1.27,<2.0.0",
        "requests": "~=2.32",
        "pinned": "==1.2.3",
        "wild": "==1.28.*",
        "bare": "",
    }


def test_pyproject_extras_with_space(tmp_path: Path) -> None:
    specs = pm.pyproject_dependency_specs(_write_pyproject(tmp_path, ["mcp [cli] >=1.27,<2"]))
    assert specs == {"mcp": ">=1.27,<2"}


def test_pyproject_rejects_unmirrorable(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="direct-URL"):
        pm.pyproject_dependency_specs(_write_pyproject(tmp_path, ["x @ git+https://x/y"]))
    with pytest.raises(ManifestError, match="environment marker"):
        pm.pyproject_dependency_specs(_write_pyproject(tmp_path, ["x>=1; python_version > '3.12'"]))
    with pytest.raises(ManifestError, match="unsupported clause"):
        pm.pyproject_dependency_specs(_write_pyproject(tmp_path, ["x!=1.0"]))
    with pytest.raises(ManifestError, match="duplicate"):
        pm.pyproject_dependency_specs(_write_pyproject(tmp_path, ["x>=1,<2", "x>=1,<3"]))


def test_pyproject_missing(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match=r"no pyproject\.toml"):
        pm.pyproject_dependency_specs(tmp_path)


# ── load_manifest ──────────────────────────────────────────────────────


def test_load_manifest_missing_file_is_none(tmp_path: Path) -> None:
    assert pm.load_manifest(tmp_path) is None


def test_load_manifest_invalid_json(tmp_path: Path) -> None:
    (tmp_path / "ava-plugin.json").write_text("not json", encoding="utf-8")
    with pytest.raises(ManifestError, match="not valid JSON"):
        pm.load_manifest(tmp_path)


def test_load_manifest_non_object(tmp_path: Path) -> None:
    (tmp_path / "ava-plugin.json").write_text("[1]", encoding="utf-8")
    with pytest.raises(ManifestError, match="JSON object"):
        pm.load_manifest(tmp_path)


def test_load_manifest_roundtrip(tmp_path: Path) -> None:
    (tmp_path / "ava-plugin.json").write_text(
        '{"apiVersion": 2, "name": "acme", "version": "1.0.0", '
        '"engines": {"ava": ">=0.1.0"}, "dependencies": {"pythonPackages": ["mcp>=1.27,<2"]}}',
        encoding="utf-8",
    )
    m = pm.load_manifest(tmp_path)
    assert m is not None
    assert m.name == "acme"
    assert m.dependencies.python_packages == {"mcp": ">=1.27,<2"}


def test_mirror_declared_lower_py_none() -> None:
    errors = _mirror(">=1.27,<=2", "<2")
    assert len(errors) == 1
    assert "no lower bound" in errors[0]


def test_mirror_tilde_patch_level() -> None:
    assert _mirror(">=1.27,<1.28", "~=1.27.3") == []
    assert _mirror(">=1.27,<1.28", "~=1.27") != []  # ~=1.27 allows up to 2.0


def test_pyproject_wildcard_major_only(tmp_path: Path) -> None:
    specs = pm.pyproject_dependency_specs(_write_pyproject(tmp_path, ["x==1.*"]))
    assert specs == {"x": "==1.*"}
    assert _mirror(">=1.0,<2", "==1.*") == []
