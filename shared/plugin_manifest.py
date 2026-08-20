"""`ava-plugin.json` manifest — parse, validate, range algebra, pyproject mirror.

The contract layer of plugin spec v2 (S0-S2, task #1244). Normative spec:
`conventions/plugin-spec-v2.md`. Install-time only: nothing here runs at agent
runtime, and packages without a manifest keep flowing through the legacy
detection paths untouched.

A package root may carry `ava-plugin.json` declaring identity
(name/version/`engines.ava` range), dependencies (`plugins` /
`pythonPackages` / `hostCapabilities`), contribution surfaces, and lifecycle
shape. This module turns that file into a validated `PluginManifest` and
provides the install-time enforcement checks:

- `check_host_engine` — the host's Ava version must satisfy `engines.ava`.
- `check_python_packages` — for MCP packages, `pyproject.toml` direct
  dependencies are the *mirror* of `dependencies.pythonPackages`: the same
  names, each pyproject range inside the declared range. An unbounded declared
  range is a hard validation error (user ruling 2026-08-13: the #1198 shape
  must fail at validation, not at install).

Version ranges are conjunctions of clauses (`,` or whitespace separated) over
`>=`, `>`, `<=`, `<`, `==`, `=`; a bare version means `==`. No OR, no
wildcards, no `~=`/`!=` — and no prerelease ordering: prerelease suffixes are
accepted on versions but compare as the release (deliberately conservative,
documented in the spec).
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from shared.plugin_ui_contributions import validate_ui_contributions

MANIFEST_FILENAME = "ava-plugin.json"
API_VERSION = 2

# ── errors ──────────────────────────────────────────────────────────────


class ManifestError(Exception):
    """Manifest content / validation failure, with all errors joined."""


# ── versions ────────────────────────────────────────────────────────────

_VERSION_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?$")


@dataclass(frozen=True, order=True)
class _Version:
    """Three-segment version; prerelease compares as its release (no ordering)."""

    major: int
    minor: int = 0
    patch: int = 0
    prerelease: str | None = field(default=None, compare=False)


def _parse_version(raw: str) -> _Version | None:
    m = _VERSION_RE.match(raw.strip())
    if m is None:
        return None
    return _Version(int(m[1]), int(m[2] or 0), int(m[3] or 0), m[4])


# ── ranges ──────────────────────────────────────────────────────────────

_MANIFEST_OPS = ("==", ">=", "<=", ">", "<", "=")
# Ops a manifest range clause may use. `~=` / `!=` / wildcards are rejected:
# the manifest grammar is deliberately the narrow, auditable subset the spec
# defines; pyproject specifiers outside the same subset (plus `~=`, `==X.Y.*`)
# are refused by `_parse_py_spec` below rather than half-analyzed.
_UPPER_OPS = ("<", "<=", "==")
_LOWER_OPS = (">", ">=", "==")

_CLAUSE_SPLIT_RE = re.compile(r"[,\s]+")


@dataclass(frozen=True)
class _Clause:
    op: str  # normalized: "=" folded to "=="
    version: _Version


def parse_range(raw: str) -> tuple[_Clause, ...]:
    """Parse a manifest range; raises `ManifestError` on anything unsupported.

    A clause against a version that fails to parse is refused too — a range is
    a contract, and a typo'd version must not silently read as "any".
    """
    parts = [p for p in _CLAUSE_SPLIT_RE.split(raw.strip()) if p]
    if not parts:
        raise ManifestError(f"empty version range {raw!r}")
    clauses: list[_Clause] = []
    for part in parts:
        op = next((o for o in _MANIFEST_OPS if part.startswith(o)), None)
        rest = part
        if op is None:
            # a bare version is a `==` clause (the spec's "a bare version
            # means ==") — refused only when it is not a version at all
            op = "=="
        else:
            rest = part[len(op) :]
        ver = _parse_version(rest)
        if ver is None:
            raise ManifestError(f"range {raw!r}: {rest!r} is not a version")
        clauses.append(_Clause("==" if op == "=" else op, ver))
    return tuple(clauses)


def _has_upper(clauses: tuple[_Clause, ...]) -> bool:
    return any(c.op in _UPPER_OPS for c in clauses)


def range_allows(raw: str, version: str) -> bool:
    """True when `version` satisfies every clause of the range."""
    ver = _parse_version(version)
    if ver is None:
        raise ManifestError(f"{version!r} is not a version")
    for clause in parse_range(raw):
        c = clause.version
        ok = {
            "==": ver == c,
            ">=": ver >= c,
            "<=": ver <= c,
            ">": ver > c,
            "<": ver < c,
        }[clause.op]
        if not ok:
            return False
    return True


# ── pyproject dependency specs ──────────────────────────────────────────

_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)")


@dataclass(frozen=True)
class _Bounds:
    """One side of a pyproject specifier set: the strictest clause it implies."""

    op: str  # ">", ">=", "==", "<=", "<"
    version: _Version


def _parse_py_spec(spec: str) -> tuple[list[_Bounds], list[_Bounds]]:
    """Parse a pyproject specifier string into (lower, upper) bound lists.

    Supports the manifest ops plus `~=` and `==X.Y.*` (the PEP 440 shapes a
    real pyproject uses). Raises `ManifestError` on `!=`, `===`, wildcards
    beyond `==X.Y.*`, or unparseable parts — refusing beats half-analyzing.
    """
    lowers: list[_Bounds] = []
    uppers: list[_Bounds] = []
    for part in [p for p in _CLAUSE_SPLIT_RE.split(spec.strip()) if p]:
        if part.startswith("==") and part.endswith(".*"):
            core = part[2:-2].rstrip(".")
            ver = _parse_version(core)
            if ver is None:
                raise ManifestError(f"specifier {spec!r}: bad wildcard {part!r}")
            lowers.append(_Bounds(">=", ver))
            segments = len(core.split("."))
            if segments == 1:
                uppers.append(_Bounds("<", _Version(ver.major + 1, 0, 0)))
            else:
                uppers.append(_Bounds("<", _Version(ver.major, ver.minor + 1, 0)))
            continue
        op = next((o for o in ("==", ">=", "<=", "~=", ">", "<", "=") if part.startswith(o)), None)
        if op is None:
            raise ManifestError(f"specifier {spec!r}: unsupported clause {part!r}")
        if op == "~=":
            ver = _parse_version(part[2:])
            if ver is None:
                raise ManifestError(f"specifier {spec!r}: bad ~= version")
            lowers.append(_Bounds(">=", ver))
            if ver.patch:
                uppers.append(_Bounds("<", _Version(ver.major, ver.minor + 1, 0)))
            else:
                uppers.append(_Bounds("<", _Version(ver.major + 1, 0, 0)))
            continue
        if op == "=":
            op = "=="
        ver = _parse_version(part[len(op) :])
        if ver is None:
            raise ManifestError(f"specifier {spec!r}: {part[len(op) :]!r} is not a version")
        if op == "==":
            lowers.append(_Bounds(op, ver))
            uppers.append(_Bounds(op, ver))
        elif op in (">", ">="):
            lowers.append(_Bounds(op, ver))
        else:
            uppers.append(_Bounds(op, ver))
    return lowers, uppers


def _strictest_lower(bounds: list[_Bounds]) -> _Bounds | None:
    """The lower bound admitting the fewest versions: highest version; on a
    tie prefer the exclusive form (`>` — it excludes the boundary itself)."""
    if not bounds:
        return None
    best = bounds[0]
    for b in bounds[1:]:
        if b.version > best.version or (
            b.version == best.version and b.op == ">" and best.op != ">"
        ):
            best = b
    return best


def _strictest_upper(bounds: list[_Bounds]) -> _Bounds | None:
    """The upper bound admitting the fewest versions: lowest version; on a tie
    prefer the exclusive form (`<`)."""
    if not bounds:
        return None
    best = bounds[0]
    for b in bounds[1:]:
        if b.version < best.version or (
            b.version == best.version and b.op == "<" and best.op != "<"
        ):
            best = b
    return best


def pyproject_dependency_specs(pkg_dir: Path) -> dict[str, str]:
    """Direct `[project].dependencies` of the pyproject in `pkg_dir`, as
    `{name: specifier_string}` ("" = no specifier, i.e. any version).

    Raises `ManifestError` on a missing/unparseable pyproject, a duplicate
    dependency name, a direct-URL dependency (`pkg @ url`), an environment
    marker (`; ...`), or an unsupported specifier — the mirror check must see
    the whole truth or nothing.
    """
    pyproject = pkg_dir / "pyproject.toml"
    if not pyproject.is_file():
        raise ManifestError(f"no pyproject.toml in {pkg_dir}")
    try:
        data: dict[str, Any] = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(f"cannot parse {pyproject}: {e}") from e
    project: dict[str, Any] | None = data.get("project")
    if project is None:
        raise ManifestError(f"{pyproject}: [project] section missing or not a table")
    deps = cast(list[Any], project.get("dependencies", []))
    if not isinstance(deps, list):
        raise ManifestError(f"{pyproject}: [project].dependencies is not a list")
    specs: dict[str, str] = {}
    for raw_entry in deps:
        if not isinstance(raw_entry, str) or not raw_entry.strip():
            raise ManifestError(f"{pyproject}: bad dependency entry {raw_entry!r}")
        entry = raw_entry.strip()
        m = _NAME_RE.match(entry)
        if m is None:
            raise ManifestError(f"{pyproject}: cannot read a package name from {entry!r}")
        name = m[1]
        rest = entry[m.end() :].strip()
        if name in specs:
            raise ManifestError(f"{pyproject}: duplicate dependency {name!r}")
        if rest.startswith("["):
            close = rest.find("]")
            if close < 0:
                raise ManifestError(f"{pyproject}: bad extras in {entry!r}")
            rest = rest[close + 1 :].strip()
        if "@" in rest:
            raise ManifestError(
                f"{pyproject}: {name!r} is a direct-URL dependency; the manifest "
                "mirror needs a versioned requirement"
            )
        if ";" in rest:
            raise ManifestError(
                f"{pyproject}: {name!r} carries an environment marker; the "
                "manifest mirror does not model markers — declare it as a plain "
                "versioned requirement"
            )
        if rest:
            _parse_py_spec(rest)  # syntax gate: refuse what we cannot analyze
        specs[name] = rest
    return specs


# ── the manifest ────────────────────────────────────────────────────────

_NAME_FORMAT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")

HOOK_POINTS = ("after_init", "before_llm", "before_exec", "after_exec")
CONTRIBUTION_KEYS = {
    "hooks",
    "sdkNamespaces",
    "sdkWraps",
    "systemPromptSections",
    "opsServices",
    "config",
    "skills",
    "commands",
    "mcpServers",
    "ui",
}
HOST_CAPABILITIES = {
    "db": ("none", "ro", "rw"),
    "network": ("none", "local", "any"),
    "shell": ("none", "any"),
    "display": ("none", "required"),
    "unixSocket": ("none", "required"),
}


@dataclass(frozen=True)
class Lifecycle:
    entry: str | None
    activation: str  # "immediate" — the only value accepted today
    dispose: str  # "effect-registry" | "none"


@dataclass(frozen=True)
class Dependencies:
    plugins: dict[str, str]
    python_packages: dict[str, str]
    host_capabilities: dict[str, str]


@dataclass(frozen=True)
class PluginManifest:
    name: str
    version: str
    engines: dict[str, str]
    description: str | None
    contributions: dict[str, object]
    dependencies: Dependencies
    lifecycle: Lifecycle


def _str_list(value: object, what: str, errors: list[str]) -> list[str] | None:
    if not isinstance(value, list):
        errors.append(f"{what}: expected a list of non-empty strings")
        return None
    values = cast(list[Any], value)
    items = [v for v in values if isinstance(v, str) and v]
    if len(items) != len(values):
        errors.append(f"{what}: expected a list of non-empty strings")
        return None
    return items


def _validate_identity(
    data: dict[str, Any], errors: list[str]
) -> tuple[str | None, str | None, str | None]:
    """apiVersion / unknown top-level keys / name / version / description."""
    known = {
        "apiVersion",
        "name",
        "version",
        "description",
        "engines",
        "contributions",
        "dependencies",
        "lifecycle",
    }
    for key in data:
        if key not in known:
            errors.append(f"unknown field {key!r}")

    if data.get("apiVersion") != API_VERSION:
        errors.append(
            f"apiVersion must be {API_VERSION} (this Ava only knows spec v2); "
            f"got {data.get('apiVersion')!r}"
        )

    name = data.get("name")
    if not isinstance(name, str) or _NAME_FORMAT_RE.match(name) is None:
        errors.append(
            f"name must match ^[a-z0-9][a-z0-9_.-]*$ (lowercase, dash-folded to "
            f"the directory name); got {name!r}"
        )

    version = data.get("version")
    if not isinstance(version, str) or _parse_version(version) is None:
        errors.append(f"version must be semver (X.Y.Z, optional -prerelease); got {version!r}")

    description = data.get("description")
    if description is not None and not isinstance(description, str):
        errors.append("description must be a string")
    return name, version, description


def _validate_engines(data: dict[str, Any], errors: list[str]) -> dict[str, str]:
    """`engines` — required, and "ava" is the only known host today."""
    engines = cast(dict[str, Any] | None, data.get("engines"))
    parsed: dict[str, str] = {}
    if engines is None:
        errors.append(
            'engines is required and must be an object, e.g. {"ava": ">=0.1.38"}; got {engines!r}'
        )
        return parsed
    for host, raw_range in engines.items():
        if host != "ava":
            errors.append(f"engines: unknown host {host!r} (only 'ava')")
            continue
        if not isinstance(raw_range, str):
            errors.append(f"engines.ava must be a version range string; got {raw_range!r}")
            continue
        try:
            parse_range(raw_range)
        except ManifestError as e:
            errors.append(f"engines.ava: {e}")
        else:
            parsed[host] = raw_range
    return parsed


def _validate_contributions(data: dict[str, Any], errors: list[str]) -> dict[str, object]:
    """`contributions` — declared surfaces; the declaration is documentation,
    the registration-vs-declaration runtime diff lands with S3."""
    contributions = cast(dict[str, Any] | None, data.get("contributions"))
    parsed: dict[str, object] = {}
    if contributions is None:
        return parsed
    for key, value in contributions.items():
        if key not in CONTRIBUTION_KEYS:
            errors.append(f"contributions: unknown surface {key!r}")
            continue
        if key == "config":
            if not isinstance(value, dict):
                errors.append("contributions.config must be an object {schema?, perAgentFields?}")
                continue
            cfg: dict[str, object] = {}
            value = cast(dict[str, Any], value)
            for ckey, cval in value.items():
                if ckey not in ("schema", "perAgentFields"):
                    errors.append(f"contributions.config: unknown field {ckey!r}")
                elif ckey == "schema" and not isinstance(cval, str):
                    errors.append("contributions.config.schema must be a string path")
                elif ckey == "perAgentFields":
                    lst = _str_list(cval, "contributions.config.perAgentFields", errors)
                    if lst is not None:
                        cfg[ckey] = lst
                else:
                    cfg[ckey] = cval
            parsed[key] = cfg
            continue
        if key == "ui":
            parsed[key] = validate_ui_contributions(value, errors)
            continue
        lst = _str_list(value, f"contributions.{key}", errors)
        if lst is None:
            continue
        if key == "hooks":
            for hook in lst:
                if hook not in HOOK_POINTS:
                    errors.append(
                        f"contributions.hooks: unknown hook point {hook!r} "
                        f"(one of {', '.join(HOOK_POINTS)})"
                    )
        parsed[key] = lst
    return parsed


def _validate_plugins(dependencies: dict[str, Any], errors: list[str]) -> dict[str, str]:
    plugins = cast(dict[str, Any] | None, dependencies.get("plugins"))
    parsed: dict[str, str] = {}
    if plugins is None:
        return parsed
    for pname, raw_range in plugins.items():
        if _NAME_FORMAT_RE.match(str(pname)) is None:
            errors.append(f"dependencies.plugins: bad package name {pname!r}")
            continue
        if not isinstance(raw_range, str):
            errors.append(f"dependencies.plugins.{pname}: range must be a string")
            continue
        try:
            parse_range(raw_range)
        except ManifestError as e:
            errors.append(f"dependencies.plugins.{pname}: {e}")
        else:
            parsed[pname] = raw_range
    return parsed


def _validate_python_packages(dependencies: dict[str, Any], errors: list[str]) -> dict[str, str]:
    packages = cast(list[Any] | None, dependencies.get("pythonPackages"))
    parsed: dict[str, str] = {}
    if packages is None:
        return parsed
    for raw_entry in packages:
        if not isinstance(raw_entry, str) or not raw_entry.strip():
            errors.append("dependencies.pythonPackages entries must be non-empty strings")
            continue
        entry = raw_entry.strip()
        m = _NAME_RE.match(entry)
        if m is None:
            errors.append(f"dependencies.pythonPackages: cannot read a package name from {entry!r}")
            continue
        pname = m[1]
        rest = entry[m.end() :].strip()
        if rest.startswith("["):
            close = rest.find("]")
            if close < 0:
                errors.append(f"dependencies.pythonPackages: bad extras in {entry!r}")
                continue
            rest = rest[close + 1 :].strip()
        if not rest:
            errors.append(
                f"dependencies.pythonPackages: {entry!r} has no range — "
                "every entry must declare an upper bound (user ruling "
                "2026-08-13: unbounded is the #1198 shape)"
            )
            continue
        try:
            clauses = parse_range(rest)
        except ManifestError as e:
            errors.append(f"dependencies.pythonPackages: {entry!r}: {e}")
            continue
        if not _has_upper(clauses):
            errors.append(
                f"dependencies.pythonPackages: {entry!r} has no upper bound "
                "(no <, <=, or == clause) — required, hard enforcement "
                "(user ruling 2026-08-13)"
            )
            continue
        if pname in parsed:
            errors.append(f"dependencies.pythonPackages: duplicate entry {pname!r}")
            continue
        parsed[pname] = rest
    return parsed


def _validate_host_capabilities(dependencies: dict[str, Any], errors: list[str]) -> dict[str, str]:
    host_caps = cast(dict[str, Any] | None, dependencies.get("hostCapabilities"))
    parsed: dict[str, str] = {}
    if host_caps is None:
        return parsed
    for ckey, cval in host_caps.items():
        allowed = HOST_CAPABILITIES.get(str(ckey))
        if allowed is None:
            errors.append(
                f"dependencies.hostCapabilities: unknown capability {ckey!r} "
                f"(one of {', '.join(sorted(HOST_CAPABILITIES))})"
            )
        elif cval not in allowed:
            errors.append(
                f"dependencies.hostCapabilities.{ckey}: {cval!r} is not one of {'/'.join(allowed)}"
            )
        else:
            parsed[ckey] = cval
    return parsed


def _validate_dependencies(data: dict[str, Any], errors: list[str]) -> Dependencies:
    dependencies = cast(dict[str, Any] | None, data.get("dependencies"))
    if dependencies is None:
        return Dependencies(plugins={}, python_packages={}, host_capabilities={})
    for dkey in dependencies:
        if dkey not in ("plugins", "pythonPackages", "hostCapabilities"):
            errors.append(f"dependencies: unknown field {dkey!r}")
    return Dependencies(
        plugins=_validate_plugins(dependencies, errors),
        python_packages=_validate_python_packages(dependencies, errors),
        host_capabilities=_validate_host_capabilities(dependencies, errors),
    )


def _validate_lifecycle(data: dict[str, Any], errors: list[str]) -> Lifecycle:
    """`lifecycle` — the declared shape; only "immediate" activation exists today."""
    lifecycle = cast(dict[str, Any] | None, data.get("lifecycle"))
    if lifecycle is None:
        return Lifecycle(entry=None, activation="immediate", dispose="effect-registry")
    for lkey in lifecycle:
        if lkey not in ("entry", "activation", "dispose"):
            errors.append(f"lifecycle: unknown field {lkey!r}")
    entry = lifecycle.get("entry")
    if entry is not None and not isinstance(entry, str):
        errors.append("lifecycle.entry must be a string path")
    activation = lifecycle.get("activation", "immediate")
    if activation != "immediate":
        errors.append(
            f"lifecycle.activation: {activation!r} unsupported — only 'immediate' exists today"
        )
    dispose = lifecycle.get("dispose", "effect-registry")
    if dispose not in ("effect-registry", "none"):
        errors.append(f"lifecycle.dispose: {dispose!r} unsupported — 'effect-registry' or 'none'")
    return Lifecycle(
        entry=entry if isinstance(entry, str) else None,
        activation=activation,
        dispose=dispose,
    )


def _validate(data: dict[str, Any]) -> PluginManifest:
    """Validate a parsed manifest dict; raises `ManifestError` listing every
    problem found (one report instead of first-error iteration)."""
    errors: list[str] = []
    name, version, description = _validate_identity(data, errors)
    parsed_engines = _validate_engines(data, errors)
    parsed_contributions = _validate_contributions(data, errors)
    parsed_deps = _validate_dependencies(data, errors)
    parsed_lifecycle = _validate_lifecycle(data, errors)

    if errors:
        raise ManifestError("\n".join(f"- {e}" for e in errors))
    return PluginManifest(
        name=cast(str, name),
        version=cast(str, version),
        engines=parsed_engines,
        description=description,
        contributions=parsed_contributions,
        dependencies=parsed_deps,
        lifecycle=parsed_lifecycle,
    )


def load_manifest(pkg_dir: Path) -> PluginManifest | None:
    """Load and validate `pkg_dir`/`ava-plugin.json`; None when the package
    ships no manifest (the legacy detection paths take over).

    Raises `ManifestError` listing every problem — invalid JSON, unknown
    fields, bad ranges, unbounded pythonPackages entries. A manifest is a
    contract: a package that ships one either validates or is refused.
    """
    path = pkg_dir / MANIFEST_FILENAME
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ManifestError(f"cannot read {path}: {e}") from e
    try:
        data = cast(dict[str, Any], json.loads(raw))
    except json.JSONDecodeError as e:
        raise ManifestError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: manifest must be a JSON object; got {type(data).__name__}")
    return _validate(data)


def host_version_from_repo(repo: Path) -> str:
    """The Ava version this checkout carries (`[project].version` in its
    pyproject.toml) — the host version `engines.ava` is checked against."""
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        raise ManifestError(f"cannot determine host version: no pyproject.toml in {repo}")
    try:
        data: dict[str, Any] = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(f"cannot determine host version: {e}") from e
    project: dict[str, Any] | None = data.get("project")
    version = project.get("version") if project is not None else None
    if not isinstance(version, str) or _parse_version(version) is None:
        raise ManifestError(f"cannot determine host version: {pyproject} [project].version invalid")
    return version


# ── install-time checks ─────────────────────────────────────────────────


def check_host_engine(manifest: PluginManifest, host_version: str) -> list[str]:
    """Errors when the host version falls outside any declared engine range."""
    errors: list[str] = []
    for _host, raw_range in manifest.engines.items():
        if not range_allows(raw_range, host_version):
            errors.append(
                f"engines: this package requires Ava {raw_range} but the host "
                f"checkout is {host_version}"
            )
    return errors


def _pick_bound(clauses: tuple[_Clause, ...], side: str) -> _Clause | None:
    """The declared-range bound admitting the fewest versions on `side`.

    Lower: highest version, ties to the exclusive form (`>`). Upper: lowest
    version, ties to `<`. `==` counts as a bound on both sides (it pins the
    version exactly).
    """
    best: _Clause | None = None
    for clause in clauses:
        if side == "lower" and clause.op not in _LOWER_OPS:
            continue
        if side == "upper" and clause.op not in _UPPER_OPS:
            continue
        if best is None:
            best = clause
            continue
        if side == "lower":
            if clause.version > best.version or (
                clause.version == best.version and clause.op == ">" and best.op != ">"
            ):
                best = clause
        elif clause.version < best.version or (
            clause.version == best.version and clause.op == "<" and best.op != "<"
        ):
            best = clause
    return best


def check_python_packages(manifest: PluginManifest, specs: dict[str, str]) -> list[str]:
    """Mirror check: pyproject direct dependencies must be exactly the declared
    `pythonPackages`, and each pyproject range must sit inside the declared
    range. Returns every violation; refusal is all-or-nothing.

    An unbounded pyproject range is the exact #1198 shape and is refused here
    even though the declared range itself has an upper bound.
    """
    declared = manifest.dependencies.python_packages
    errors: list[str] = []
    for name in declared:
        if name not in specs:
            errors.append(
                f"pythonPackages: manifest declares {name!r} but pyproject.toml "
                "[project].dependencies has no such entry"
            )
    for name, spec in specs.items():
        if name not in declared:
            errors.append(
                f"pythonPackages: pyproject.toml declares {name!r} but the manifest "
                "does not — dependencies.pythonPackages is the mirror of direct "
                "dependencies"
            )
            continue
        raw_range = declared[name]
        try:
            declared_clauses = parse_range(raw_range)
            py_lowers, py_uppers = _parse_py_spec(spec) if spec else ([], [])
        except ManifestError as e:
            errors.append(f"pythonPackages: {name!r}: {e}")
            continue
        d_lower = _pick_bound(declared_clauses, "lower")
        d_upper = _pick_bound(declared_clauses, "upper")
        py_lower = _strictest_lower(py_lowers)
        py_upper = _strictest_upper(py_uppers)

        for side, py_bound, d_bound in (
            ("lower", py_lower, d_lower),
            ("upper", py_upper, d_upper),
        ):
            err = _side_error(name, spec, raw_range, side, py_bound, d_bound)
            if err is not None:
                errors.append(err)
    return errors


def _side_error(
    name: str,
    spec: str,
    raw_range: str,
    side: str,
    py_bound: _Bounds | None,
    d_bound: _Clause | None,
) -> str | None:
    """None when the pyproject bound sits inside the declared range on `side`."""
    if d_bound is None:
        if py_bound is None:
            return None
        return (
            f"pythonPackages: {name!r}: pyproject.toml {spec!r} constrains the "
            f"{side} end but the declared range {raw_range!r} does not"
        )
    if py_bound is None:
        if side == "upper":
            return (
                f"pythonPackages: {name!r}: pyproject.toml {spec!r} has no upper "
                f"bound — this is the #1198 failure shape; the declared range "
                f"is {raw_range!r}"
            )
        return (
            f"pythonPackages: {name!r}: pyproject.toml {spec!r} has no lower "
            f"bound but the declared range {raw_range!r} does"
        )
    dv, pv = d_bound.version, py_bound.version
    d_excl = d_bound.op in (">", "<")
    p_excl = py_bound.op in (">", "<")
    if side == "lower":
        if pv > dv or (pv == dv and not (d_excl and not p_excl)):
            return None
        return (
            f"pythonPackages: {name!r}: pyproject.toml {spec!r} reaches below "
            f"the declared range {raw_range!r}"
        )
    if pv < dv or (pv == dv and not (p_excl is False and d_excl is True)):
        return None
    return (
        f"pythonPackages: {name!r}: pyproject.toml {spec!r} reaches above "
        f"the declared range {raw_range!r}"
    )
