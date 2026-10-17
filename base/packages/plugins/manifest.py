from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from base import paths
from base.host.git import host_version as host_version_module
from base.host.git.host_version import Version, parse_version
from base.packages.plugins.surface.ui_contributions import validate_ui_contributions

MANIFEST_FILENAME = "meiva-plugin.json"
API_VERSION = 2


class ManifestError(Exception):
    pass


_MANIFEST_OPS = ("==", ">=", "<=", ">", "<", "=")
UPPER_OPS = ("<", "<=", "==")
LOWER_OPS = (">", ">=", "==")

CLAUSE_SPLIT_RE = re.compile(r"[,\s]+")


@dataclass(frozen=True)
class Clause:
    op: str
    version: Version


# meiva:ref base/packages/plugins/docs/manifest.meiva.okf.md#closed-manifest-grammar
def parse_range(raw: str) -> tuple[Clause, ...]:
    parts = [p for p in CLAUSE_SPLIT_RE.split(raw.strip()) if p]
    if not parts:
        raise ManifestError(f"empty version range {raw!r}")
    clauses: list[Clause] = []
    for part in parts:
        op = next((o for o in _MANIFEST_OPS if part.startswith(o)), None)
        rest = part
        if op is None:
            op = "=="
        else:
            rest = part[len(op) :]
        ver = parse_version(rest)
        if ver is None:
            raise ManifestError(f"range {raw!r}: {rest!r} is not a version")
        clauses.append(Clause("==" if op == "=" else op, ver))
    return tuple(clauses)


def _has_upper(clauses: tuple[Clause, ...]) -> bool:
    return any(c.op in UPPER_OPS for c in clauses)


# meiva:ref base/packages/plugins/docs/manifest.meiva.okf.md#closed-manifest-grammar
def range_allows(raw: str, version: str) -> bool:
    ver = parse_version(version)
    if ver is None:
        raise ManifestError(f"{version!r} is not a version")
    clauses = parse_range(raw)
    if ver.prerelease is not None and not any(c.version.prerelease for c in clauses):
        return False
    for clause in clauses:
        c = clause.version
        if (
            clause.op == "<"
            and ver.prerelease is not None
            and c.prerelease is None
            and (ver.major, ver.minor, ver.patch) == (c.major, c.minor, c.patch)
        ):
            return False
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


_NAME_FORMAT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)")

HOOK_POINTS = ("after_init", "before_llm", "before_exec", "after_exec")
CONTRIBUTION_KEYS = {
    "hooks",
    "sdkNamespaces",
    "sdkWraps",
    "systemPromptSections",
    "compactReminderSections",
    "metrics",
    "inspectWidgets",
    "providers",
    "decisionProviders",
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
    activation: str
    dispose: str


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
            f"apiVersion must be {API_VERSION} (this Meiva only knows spec v2); "
            f"got {data.get('apiVersion')!r}"
        )

    name = data.get("name")
    if not isinstance(name, str) or _NAME_FORMAT_RE.match(name) is None:
        errors.append(
            f"name must match ^[a-z0-9][a-z0-9_.-]*$ (lowercase, dash-folded to "
            f"the directory name); got {name!r}"
        )

    version = data.get("version")
    if not isinstance(version, str) or parse_version(version) is None:
        errors.append(f"version must be semver (X.Y.Z, optional -prerelease); got {version!r}")

    description = data.get("description")
    if description is not None and not isinstance(description, str):
        errors.append("description must be a string")
    return name, version, description


def _validate_engines(data: dict[str, Any], errors: list[str]) -> dict[str, str]:
    engines = cast(dict[str, Any] | None, data.get("engines"))
    parsed: dict[str, str] = {}
    if engines is None:
        if data.get("requires_commit") is None:
            errors.append(
                "engines (or requires_commit) is required; engines must be an "
                'object, e.g. {"meiva": ">=0.1.38"}'
            )
        return parsed
    for host, raw_range in engines.items():
        if host != "meiva":
            errors.append(f"engines: unknown host {host!r} (only 'meiva')")
            continue
        if not isinstance(raw_range, str):
            errors.append(f"engines.meiva must be a version range string; got {raw_range!r}")
            continue
        try:
            parse_range(raw_range)
        except ManifestError as e:
            errors.append(f"engines.meiva: {e}")
        else:
            parsed[host] = raw_range
    return parsed


_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _validate_requires_commit(data: dict[str, Any], errors: list[str]) -> str | None:
    raw = data.get("requires_commit")
    if raw is None:
        return None
    if not isinstance(raw, str) or _COMMIT_RE.match(raw) is None:
        errors.append(f"requires_commit must be a lowercase hex commit SHA; got {raw!r}")
        return None
    return raw


def _config_contribution(value: object, errors: list[str]) -> dict[str, object] | None:
    if not isinstance(value, dict):
        errors.append("contributions.config must be an object {schema?, perAgentFields?}")
        return None
    cfg: dict[str, object] = {}
    for ckey, cval in cast(dict[str, Any], value).items():
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
    return cfg


def _check_hook_points(hooks: list[str], errors: list[str]) -> None:
    for hook in hooks:
        if hook not in HOOK_POINTS:
            errors.append(
                f"contributions.hooks: unknown hook point {hook!r} "
                f"(one of {', '.join(HOOK_POINTS)})"
            )


def _validate_contributions(data: dict[str, Any], errors: list[str]) -> dict[str, object]:
    contributions = cast(dict[str, Any] | None, data.get("contributions"))
    parsed: dict[str, object] = {}
    if contributions is None:
        return parsed
    for key, value in contributions.items():
        if key not in CONTRIBUTION_KEYS:
            errors.append(f"contributions: unknown surface {key!r}")
            continue
        if key == "config":
            cfg = _config_contribution(value, errors)
            if cfg is not None:
                parsed[key] = cfg
            continue
        if key == "ui":
            parsed[key] = validate_ui_contributions(value, errors)
            continue
        lst = _str_list(value, f"contributions.{key}", errors)
        if lst is None:
            continue
        if key == "hooks":
            _check_hook_points(lst, errors)
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
        m = NAME_RE.match(entry)
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


def validate(data: dict[str, Any]) -> PluginManifest:
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


# meiva:ref base/packages/plugins/docs/manifest.meiva.okf.md#closed-manifest-grammar
def load_manifest(pkg_dir: Path) -> PluginManifest:
    path = pkg_dir / MANIFEST_FILENAME
    if not path.is_file():
        raise ManifestError(
            f"{path} not found: every Meiva plugin ships {MANIFEST_FILENAME} at its package root. "
            f"A plugin that contributes nothing the loader gates needs only "
            f'{{"apiVersion": 2, "name": "<plugin name>", "version": "1.0.0", '
            f'"engines": {{"meiva": ">=<YYYY.M.D>"}}}}; one that does must also list them under '
            f'"contributions" (docs/conventions/extensions/plugin-spec-v2.md)'
        )
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
    return validate(data)


# meiva:ref base/packages/plugins/docs/manifest.meiva.okf.md#closed-manifest-grammar
def load_optional_manifest(pkg_dir: Path) -> PluginManifest | None:
    if not (pkg_dir / MANIFEST_FILENAME).is_file():
        return None
    return load_manifest(pkg_dir)


# meiva:ref base/packages/plugins/docs/manifest.meiva.okf.md#host-compatibility-fails-closed
def check_host_engine(manifest: PluginManifest, host_version: str) -> list[str]:
    errors: list[str] = []
    for _host, raw_range in manifest.engines.items():
        if not range_allows(raw_range, host_version):
            errors.append(
                f"engines: this package requires Meiva {raw_range} but the host "
                f"checkout is {host_version}"
            )
    return errors


_COMMIT_TIMEOUT_S = 10.0


# meiva:ref base/packages/plugins/docs/manifest.meiva.okf.md#host-compatibility-fails-closed
def host_contract_errors_dir(pkg_dir: Path, *, repo: Path | None = None) -> list[str]:
    try:
        manifest = load_optional_manifest(pkg_dir)
    except ManifestError as exc:
        return [f"manifest invalid: {exc}"]
    if manifest is None:
        return []
    target = repo or paths.repo_root()
    try:
        host = host_version_module.host_version(target)
    except host_version_module.HostVersionError as exc:
        return [str(exc)]
    return check_host_engine(manifest, host) + check_host_commit(manifest, target)


# meiva:ref base/packages/plugins/docs/manifest.meiva.okf.md#host-compatibility-fails-closed
def check_host_commit(manifest: PluginManifest, repo: Path) -> list[str]:
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
    result = _git_run(repo, *args)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_rc(repo: Path, *args: str) -> int | None:
    result = _git_run(repo, *args)
    return None if result is None else result.returncode


def _git_run(repo: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    from base.host.git.gitenv import git_env
    from base.host.proc import run_bounded

    try:
        return run_bounded(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=_COMMIT_TIMEOUT_S,
            env=git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
