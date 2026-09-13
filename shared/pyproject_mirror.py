"""pyproject.toml dependency specs + the pythonPackages mirror check (#1198).

Split out of `shared/plugin_manifest.py` here because that module reached the
line-budget ceiling: this module owns the PEP-440-ish parsing of a pyproject's
direct dependencies and the mirror check against a manifest's
`dependencies.pythonPackages`. The manifest schema/validation, the range
algebra, and the host-compatibility checks stay in `shared.plugin_manifest`.

User ruling (2026-08-13): the mirror is **hard enforcement** — a declared
range without an upper bound is a validator error (over there), and an
install/upgrade whose pyproject range falls outside the declared range is
refused here. Unbounded pyproject ranges are the exact #1198 failure shape and
are refused even when the declared range is bounded.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from shared.plugin_manifest import (
    _CLAUSE_SPLIT_RE,
    _LOWER_OPS,
    _NAME_RE,
    _UPPER_OPS,
    ManifestError,
    PluginManifest,
    _Clause,
    _parse_version,
    _Version,
    parse_range,
)

# ── pyproject dependency specs ──────────────────────────────────────────

# ── pyproject dependency specs ──────────────────────────────────────────


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


# ── the mirror check ────────────────────────────────────────────────────


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
