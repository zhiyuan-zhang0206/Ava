"""`ava-plugin.json` manifest — parse, validate, range algebra, host checks.

The contract layer of plugin spec v2 (S0-S2, task #1244). Normative spec:
`conventions/plugin-spec-v2.md`. Install-time only: nothing here runs at agent
runtime, and packages without a manifest keep flowing through the legacy
detection paths untouched.

A package root may carry `ava-plugin.json` declaring identity
(name/version/`engines.ava` range, optional `requires_commit`), dependencies
(`plugins` / `pythonPackages` / `hostCapabilities`), contribution surfaces,
and lifecycle shape. This module turns that file into a validated
`PluginManifest` and provides the host-compatibility checks:

- `check_host_engine` — the host's (derived) version must satisfy `engines.ava`.
- `check_host_commit` — the host must contain the declared `requires_commit`.
- The pyproject-mirror half of the checks (spec parsing + the
  `dependencies.pythonPackages` mirror, task #1198) lives in
  `shared/pyproject_mirror.py`.

Version ranges are conjunctions of clauses (`,` or whitespace separated) over
`>=`, `>`, `<=`, `<`, `==`, `=`; a bare version means `==`. No OR, no
wildcards, no `~=`/`!=` — and no prerelease ordering: prerelease suffixes are
accepted on versions but compare as the release (deliberately conservative,
documented in the spec).
"""

from __future__ import annotations

import json
import re
import subprocess
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


# ── the manifest ────────────────────────────────────────────────────────

_NAME_FORMAT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
# Leading package name of a PEP 508 requirement (`name[extras] >=1,<2`); shared
# with `shared/pyproject_mirror.py` (its pyproject-side parser imports this).
_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)")

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
    requires_commit: str | None = None
    """Optional exactness layer beside the date axis (design §5.5): the host
    must CONTAIN this commit (`git merge-base --is-ancestor`). A content-facing
    change that needs a fresh kernel capability points at the merge that
    introduced it — no version bump, exact within a day."""


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
        "requires_commit",
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
    """`engines` — "ava" is the only known host today. Required unless the
    manifest carries a `requires_commit` (the commit-pinned form core content
    uses; design §5.5: "engines or requires_commit")."""
    engines = cast(dict[str, Any] | None, data.get("engines"))
    parsed: dict[str, str] = {}
    if engines is None:
        if data.get("requires_commit") is None:
            errors.append(
                "engines (or requires_commit) is required; engines must be an "
                'object, e.g. {"ava": ">=0.1.38"}'
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


_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _validate_requires_commit(data: dict[str, Any], errors: list[str]) -> str | None:
    """`requires_commit` — a lowercase hex commit SHA (7-40 chars); the exact
    layer of the version gate (`check_host_commit`)."""
    raw = data.get("requires_commit")
    if raw is None:
        return None
    if not isinstance(raw, str) or _COMMIT_RE.match(raw) is None:
        errors.append(f"requires_commit must be a lowercase hex commit SHA; got {raw!r}")
        return None
    return raw


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
    parsed_requires_commit = _validate_requires_commit(data, errors)
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
        requires_commit=parsed_requires_commit,
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


_COMMIT_TIMEOUT_S = 10.0


def check_host_commit(manifest: PluginManifest, repo: Path) -> list[str]:
    """Errors when the host checkout does not CONTAIN `manifest.requires_commit`.

    The exact layer of the version gate (design §5.5): the host passes iff its
    HEAD contains the declared commit (`git merge-base --is-ancestor`). Two
    distinct failure reasons so callers can record them separately:

    - "not an ancestor": the commit is present but the host predates it — the
      canonical block; the host moving forward unblocks it.
    - "unresolvable": the commit cannot be resolved in `repo` (typo, not
      fetched, shallow clone) — still a gate failure (never a pass), but the
      diagnostic differs so the operator knows to fetch/re-check rather than
      upgrade.
    """
    sha = manifest.requires_commit
    if sha is None:
        return []
    head = _git_stdout(repo, "rev-parse", "HEAD")
    if head is None:
        return [f"requires_commit: cannot resolve HEAD in {repo} to verify {sha}"]
    present = _git_stdout(repo, "cat-file", "-e", f"{sha}^{{commit}}")
    if present is None:
        return [
            f"requires_commit: {sha} is unresolvable in {repo} — fetch it, then "
            f"retry (the host must contain it to pass the gate)"
        ]
    rc = _git_rc(repo, "merge-base", "--is-ancestor", sha, "HEAD")
    if rc == 0:
        return []
    if rc == 1:
        return [
            f"requires_commit: {sha} is not an ancestor of the host checkout's "
            f"HEAD ({head[:7]}) — the host predates the commit this package needs"
        ]
    return [f"requires_commit: ancestor check for {sha} failed in {repo} (git error)"]


def _git_stdout(repo: Path, *args: str) -> str | None:
    """stdout of a bounded read-only git call in `repo`, or None on any failure."""
    result = _git_run(repo, *args)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_rc(repo: Path, *args: str) -> int | None:
    """returncode of a bounded read-only git call in `repo`, or None when the
    call itself could not run."""
    result = _git_run(repo, *args)
    return None if result is None else result.returncode


def _git_run(repo: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    from shared.gitenv import git_env
    from shared.proc import run_bounded

    try:
        return run_bounded(  # git + fixed args, no user input
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=_COMMIT_TIMEOUT_S,
            env=git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
