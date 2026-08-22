"""`ava mcp` subcommands — manage MCP servers.

MCP is a cross-vendor standard: the same `mcpServers` entry shape that Claude
Code and Codex consume is what Ava's MCP loader merges. Two kinds of server
exist, mirroring skills: **built-in** ones ship in the repo (`<repo>/ava_builtins/mcps/`),
and **installed** ones are added out-of-core via `ava mcp install`, landing as
self-contained packages under `$AVA_HOME/mcps/<name>/` (own `.mcp.json` +
`pyproject.toml` + `.venv`) and tracked in the install registry (`type="mcp"`).
A third, ad-hoc layer is the **machine** config (`$AVA_HOME/mcp.json`, edited by
`add`/`remove`), applied last so a machine entry overrides a same-named default.

- `install <source>`              — install a standalone MCP package from a git
      `[--path SUBDIR] [--ref REF]`   URL or local dir; `uv sync` builds its
      `[--env K=V]...`                isolated venv. `--path` selects a subdir;
                                    `--env` injects machine-local values (a bot
                                    token / API key) into the landed copy's env —
                                    the only channel by which a secret reaches a
                                    spawned server, and one a package must not ship.
                                    If the package lacks `.mcp.json`, the command
                                    auto-detects from `pyproject.toml`.
- `uninstall <name>`              — remove an installed package + its registry entry.
- `upgrade <name>`                — re-fetch an installed package from its source,
                                    carrying the installed copy's env forward.
- `add <name> --json '<spec>'`    — paste a server object straight from a vendor
                                    README (`{"command": ..., "args": [...]}`).
- `add <name> --command CMD`      — build the same object from flags
      `[--arg A]... [--env K=V]...`   (convenience for simple stdio servers).
- `list` (alias `ls`)             — merged server set (built-in + plugin +
                                    installed + machine), flagging each origin
                                    and which are disabled.
- `remove <name>`                 — drop a machine-config entry (built-in /
                                    plugin / installed servers are not editable here).
- `enable <name>`                 — toggle an already-defined server on for this
                                    machine without removing its definition.
- `disable <name>`                — toggle an already-defined server off for this
                                    machine without removing its definition.

Every verb here points outward — servers Ava's own agents connect to. The one
verb pointing inward, `ava mcp serve` (this cluster's control plane exposed AS
an MCP server, for Claude Code / Codex), lives in `cli/mcp_server.py`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, cast

from cli.commands._pkg_source import AcquiredSource, SourcePathNotFoundError


def _machine_config_file() -> Path:
    from ava._mcp_config import _machine_config_path

    return _machine_config_path()


def _read_machine_config(path: Path) -> dict[str, Any]:
    """Read `$AVA_HOME/mcp.json` as a dict ({} when absent).

    Raises:
        ValueError: the file is present but not a JSON object.
    """
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        # ValueError (not TypeError): malformed config file content, surfaced to the user.
        raise ValueError(f"{path} is not a JSON object (got {type(data).__name__})")  # noqa: TRY004
    return data


def _write_machine_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _build_server_spec(
    json_spec: str | None, command: str | None, args: list[str], env_pairs: list[str]
) -> dict[str, Any]:
    """Build one server entry from either a pasted JSON object or `--command` flags.

    Raises:
        ValueError: neither/both input forms given, malformed JSON, a `--env`
            pair lacks `=`, or the result is not a server object.
    """
    if json_spec is not None and command is not None:
        raise ValueError("pass either --json or --command, not both")
    if json_spec is not None:
        parsed = json.loads(json_spec)
        if not isinstance(parsed, dict):
            raise ValueError(f"--json must be a server object (got {type(parsed).__name__})")
        return parsed
    if command is not None:
        spec: dict[str, Any] = {"command": command}
        if args:
            spec["args"] = args
        env = _parse_env_pairs(env_pairs)
        if env:
            spec["env"] = env
        return spec
    raise ValueError("need --json '<server object>' or --command CMD")


def _parse_env_pairs(env_pairs: list[str]) -> dict[str, str]:
    """Parse repeated `KEY=VALUE` flags into an env dict.

    Raises:
        ValueError: a pair lacks `=`.
    """
    env: dict[str, str] = {}
    for pair in env_pairs:
        if "=" not in pair:
            raise ValueError(f"--env expects KEY=VALUE, got {pair!r}")
        key, value = pair.split("=", 1)
        env[key] = value
    return env


def cmd_mcp_add(
    name: str,
    json_spec: str | None,
    command: str | None,
    args: list[str],
    env_pairs: list[str],
) -> int:
    """`ava mcp add <name>` — add/replace a server in the machine MCP config."""
    path = _machine_config_file()
    try:
        spec = _build_server_spec(json_spec, command, args, env_pairs)
        config = _read_machine_config(path)
    except (ValueError, json.JSONDecodeError, OSError) as e:
        print(f"[ava mcp add] {e}", file=sys.stderr)
        return 1

    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        print(f"[ava mcp add] {path} mcpServers field is not an object.", file=sys.stderr)
        return 1
    replaced = name in servers
    servers[name] = spec
    _write_machine_config(path, config)

    verb = "replaced" if replaced else "added"
    print(f"[ava mcp add] {verb} MCP server '{name}' in {path}")
    print("  connects on next use; no restart needed.")
    return 0


def _server_summary(spec: Any) -> str:
    """One-line description of a server entry for the list view."""
    if not isinstance(spec, dict):
        return "(malformed entry)"
    spec = cast(dict[str, Any], spec)
    if "url" in spec:
        return str(spec["url"])
    command = spec.get("command", "")
    args: list[Any] = spec.get("args", [])
    parts = [str(command), *(str(a) for a in args)] if isinstance(args, list) else [str(command)]
    return " ".join(p for p in parts if p)


def cmd_mcp_list() -> int:
    """`ava mcp list` — print the merged server set, flagging each server's
    origin (machine / installed / plugin-built-in) and which are disabled."""
    from ava._mcp_config import MCPError, load_mcp_config
    from shared.install_registry import installed_mcp_names
    from shared.mcp_enabled import McpEnabledConfigError, read_enabled

    try:
        merged = load_mcp_config(include_disabled=True)
        machine = _read_machine_config(_machine_config_file())
        try:
            enabled = read_enabled()
        except McpEnabledConfigError as e:
            print(f"[ava mcp list] {e} — treating every MCP server as disabled", file=sys.stderr)
            enabled = dict.fromkeys(merged, False)
        installed = installed_mcp_names()
    except (MCPError, ValueError, json.JSONDecodeError, OSError) as e:
        print(f"[ava mcp list] {e}", file=sys.stderr)
        return 1

    if not merged:
        print("[ava mcp list] (no MCP servers configured)")
        return 0
    machine_servers: dict[str, Any] = (
        machine.get("mcpServers", {}) if isinstance(machine, dict) else {}
    )
    machine_names = set(machine_servers) if isinstance(machine_servers, dict) else set()
    for name in sorted(merged):
        # Order matters: machine config is the top layer, so a name present in
        # both machine and the installed layer is served by machine.
        if name in machine_names:
            origin = "machine"
        elif name in installed:
            origin = "installed (ava mcp uninstall)"
        else:
            origin = "plugin/built-in (read-only here)"
        disabled = " [disabled]" if not enabled.get(name, True) else ""
        print(f"  {name}  [{origin}]{disabled}")
        print(f"      {_server_summary(merged[name])}")
        if name in installed and _source_is_dead_local_path(name):
            print(
                f"      ! recorded source path no longer exists — `ava mcp upgrade {name}` "
                f"will fail (reinstall from a live git URL to restore upgradeability)",
                file=sys.stderr,
            )
    return 0


def _source_is_dead_local_path(name: str) -> bool:
    """True when the installed package's recorded source is a local path that
    no longer exists on disk (an MCP installed from a git worktree that has
    since been removed — audit round 2, skills-plugins #4). Such a source
    cannot be re-fetched: `ava mcp upgrade` would fail."""
    from pathlib import Path

    from shared import install_registry

    pkg = install_registry.get(name)
    if pkg is None or pkg.source is None or "://" in pkg.source or pkg.source.startswith("git@"):
        return False
    return not Path(pkg.source).expanduser().exists()


def cmd_mcp_enable(name: str) -> int:
    """`ava mcp enable <name>` — turn an MCP server on for this machine."""
    return _set_mcp_enabled(name, enabled=True)


def cmd_mcp_disable(name: str) -> int:
    """`ava mcp disable <name>` — turn an MCP server off for this machine."""
    return _set_mcp_enabled(name, enabled=False)


def _set_mcp_enabled(name: str, *, enabled: bool) -> int:
    from ava._mcp_config import load_mcp_config
    from shared.mcp_enabled import local_config_path, set_mcp_enabled

    set_mcp_enabled(name, enabled=enabled)
    verb = "enabled" if enabled else "disabled"
    print(
        f"[ava mcp {'enable' if enabled else 'disable'}] {verb} MCP server '{name}' "
        f"in {local_config_path()}"
    )
    print("  takes effect on the next connect; no restart needed.")
    if name not in load_mcp_config(include_disabled=True):
        print(
            f"  (note: no server named '{name}' is currently defined; "
            "the override will apply if one appears)"
        )
    return 0


def cmd_mcp_remove(name: str) -> int:
    """`ava mcp remove <name>` — drop a machine-config MCP entry."""
    path = _machine_config_file()
    try:
        config = _read_machine_config(path)
    except (ValueError, json.JSONDecodeError, OSError) as e:
        print(f"[ava mcp remove] {e}", file=sys.stderr)
        return 1

    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        print(
            f"[ava mcp remove] '{name}' is not a machine-config server. "
            "Built-in / plugin-bundled servers are managed via `ava plugins`.",
            file=sys.stderr,
        )
        return 1
    del servers[name]
    _write_machine_config(path, config)
    print(f"[ava mcp remove] removed '{name}' from {path}")
    return 0


# ─── install / uninstall / upgrade (out-of-core MCP packages) ─────────────


def _package_server_name(mcp_json: Path) -> str:
    """The single server name a standalone MCP package declares under
    `mcpServers` — that key becomes the package's registry + load-dir name.

    Raises:
        ValueError: the file is unreadable, has no `mcpServers`, or does not
            declare exactly one server.
    """
    from ava._mcp_config import MCPError, _read_servers

    try:
        servers = _read_servers(mcp_json)
    except MCPError as e:
        raise ValueError(str(e)) from e
    if len(servers) != 1:
        raise ValueError(
            f"a standalone MCP package must declare exactly one server under "
            f"mcpServers; {mcp_json} declares {len(servers)}"
        )
    return next(iter(servers))


def _builtin_server_names() -> set[str]:
    """Names of the servers shipped in the repo's `<repo>/ava_builtins/mcps/` — an installed
    package may not reuse one of these (it would be shadowed)."""
    from ava._mcp_config import _builtin_mcp_paths, _read_servers

    names: set[str] = set()
    for path in _builtin_mcp_paths():
        names |= set(_read_servers(path))
    return names


def _land_package(cloned: Path | None, pkg_dir: Path, dest: Path) -> None:
    """Place the package tree at `dest`, replacing any existing copy. A cloned
    tree is moved (its temp parent is cleaned by the caller); a local source is
    copied, never moved — the user keeps their dir. `.git` / `.venv` are dropped
    so `uv sync` rebuilds a fresh venv."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if cloned is not None:
        shutil.rmtree(pkg_dir / ".git", ignore_errors=True)
        shutil.move(str(pkg_dir), str(dest))
    else:
        shutil.copytree(pkg_dir, dest, ignore=shutil.ignore_patterns(".git", ".venv"))


def _landed_env(dest: Path, name: str) -> dict[str, str]:
    """The `env` dict currently recorded for `name` in an installed package's
    `.mcp.json` ({} when the package or the key is absent).

    An installed MCP's secrets live here (see `_apply_env_overrides`), so this is
    what `upgrade` reads before re-landing in order to carry them forward.
    """
    from ava._mcp_config import MCPError, _read_servers

    mcp_json = dest / ".mcp.json"
    if not mcp_json.is_file():
        return {}
    try:
        servers = _read_servers(mcp_json)
    except MCPError:
        return {}
    env = servers.get(name, {}).get("env")
    if isinstance(env, dict):
        return dict(cast(dict[str, Any], env))
    return {}


def _apply_env_overrides(dest: Path, name: str, overrides: dict[str, str]) -> None:
    """Merge `overrides` into the landed `.mcp.json`'s `env` for `name` (override
    wins over the package's shipped default) and write the file back.

    This is how a machine-local secret reaches an installed server: the stdio
    transport passes only `get_default_environment()` plus this `env` dict, so a
    token can arrive no other way. `$AVA_HOME` is outside the repo, so the value
    never enters git — the same trust boundary as `$AVA_HOME/mcp.json`.
    """
    if not overrides:
        return
    mcp_json = dest / ".mcp.json"
    data = json.loads(mcp_json.read_text(encoding="utf-8"))
    spec = data["mcpServers"][name]
    spec["env"] = {**(spec.get("env") or {}), **overrides}
    mcp_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _uv_sync(pkg_dir: Path) -> None:
    """Build the package's isolated venv (`<pkg>/.venv`) so its relative
    `.venv/bin/python` command resolves at spawn time. `uv sync` is the only uv
    invocation an installed MCP triggers — never `uv run` (a resident wrapper).

    Raises:
        subprocess.CalledProcessError: uv sync failed.
    """
    subprocess.run(["uv", "sync"], cwd=pkg_dir, check=True, capture_output=True, text=True)


def _register_mcp(
    name: str, source: str, path: str | None, ref: str | None, *, content_hash: str | None = None
) -> None:
    """Record a freshly-installed MCP package in the registry (origin='user').

    `content_hash` / `installed_hash` record the tree hash of what was just
    written (R5): the later `upgrade` path compares the on-disk tree against
    `installed_hash` to detect local edits before overwriting.
    """
    from datetime import UTC, datetime

    from shared import install_registry

    now = datetime.now(UTC).isoformat(timespec="seconds")
    install_registry.register(
        install_registry.InstalledPackage(
            name=name,
            type="mcp",
            source=source,
            path=path,
            ref=ref,
            enabled=True,
            installed_at=now,
            updated_at=now,
            content_hash=content_hash,
            installed_hash=content_hash,
        )
    )


def _derive_server_name(raw_name: str) -> str:
    """Derive a clean server name from a pyproject ``name`` field.

    Strips common prefixes (``ava-mcp-``, ``mcp-server-``, ``mcp-``) and replaces
    underscores with hyphens so the name is consistent with built-in / machine
    server naming.
    """
    for prefix in ("ava-mcp-", "mcp-server-", "mcp-"):
        if raw_name.lower().startswith(prefix):
            raw_name = raw_name[len(prefix) :]
            break
    return raw_name.replace("_", "-").strip("-")


def _main_module_candidates(search_dirs: list[Path]) -> list[str]:
    """Return the first package with a ``__main__.py``, in search order."""
    for base in search_dirs:
        for child in sorted(base.iterdir()):
            if (
                child.is_dir()
                and not child.name.startswith(".")
                and child.name != "src"
                and (child / "__main__.py").is_file()
            ):
                return [child.name]
    return []


def _package_candidates(search_dirs: list[Path]) -> list[str]:
    """Return Python package directories in deterministic search order."""
    packages: list[str] = []
    for base in search_dirs:
        for child in sorted(base.iterdir()):
            if (
                child.is_dir()
                and not child.name.startswith(".")
                and child.name != "src"
                and (child / "__init__.py").is_file()
            ):
                packages.append(child.name)
    return packages


def _detect_module(pkg_dir: Path, project: dict[str, Any], fallback_name: str) -> str:
    """Detect the Python module to run via ``-m`` from a package tree.

    Priority (first match wins):

    1. A top-level directory (or ``src/<dir>``) whose ``__main__.py`` exists —
       the most reliable signal of a ``python -m <module>`` entry point.
    2. ``[project.scripts]`` first entry — extract the module part (left of
       ``:``) from the ``"module:function"`` value.
    3. A single top-level Python package (directory with ``__init__.py``) —
       unambiguous when only one package directory exists.
    4. *fallback_name* with underscores instead of hyphens.
    """
    search_dirs = [pkg_dir]
    src_dir = pkg_dir / "src"
    if src_dir.is_dir():
        search_dirs.append(src_dir)

    # 1. __main__.py
    main_modules = _main_module_candidates(search_dirs)
    if main_modules:
        return main_modules[0]

    # 2. [project.scripts] first entry -> module part before ':'
    scripts = project.get("scripts")
    if isinstance(scripts, dict) and scripts:
        first_value = next(iter(cast(dict[str, Any], scripts).values()))
        if isinstance(first_value, str) and ":" in first_value:
            module = first_value.split(":", 1)[0].strip()
            if module:
                return module

    # 3. Single top-level Python package (__init__.py)
    packages = _package_candidates(search_dirs)
    if len(packages) == 1:
        return packages[0]

    # 4. Fallback: sanitize the name
    return fallback_name.replace("-", "_")


def _auto_detect_mcp(pkg_dir: Path) -> tuple[str, dict[str, Any]]:
    """Auto-detect MCP server name and spec from a package that lacks ``.mcp.json``.

    Reads ``pyproject.toml`` to derive the server name and the Python module to
    run.  The returned spec dict carries ``command``, ``args``, ``env``, and
    ``description`` — the same shape as one entry under an ``.mcp.json``
    ``mcpServers`` key.

    Returns:
        ``(server_name, spec_dict)``.

    Raises:
        ValueError: ``pyproject.toml`` is missing, unparseable, or does not
            carry enough information to detect the server.
    """
    pyproject = pkg_dir / "pyproject.toml"
    if not pyproject.is_file():
        raise ValueError(
            "no .mcp.json or pyproject.toml found; "
            "an MCP package must ship at least one of these at its root"
        )
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"cannot parse pyproject.toml: {e}") from e

    project = data.get("project")
    if not isinstance(project, dict):
        raise ValueError("pyproject.toml [project] section is missing or not a table")  # noqa: TRY004
    project = cast(dict[str, Any], project)
    raw_name = project.get("name", "")
    if not raw_name:
        raise ValueError("pyproject.toml [project] has no 'name' field")
    name = _derive_server_name(str(raw_name))
    module = _detect_module(pkg_dir, project, name)
    description = str(project.get("description", "")) or f"{name} MCP server"

    spec: dict[str, Any] = {
        "command": ".venv/bin/python",
        "args": ["-m", module],
        "env": {},
        "description": description,
    }
    return name, spec


def _write_mcp_json(dest: Path, name: str, spec: dict[str, Any]) -> None:
    """Write a generated ``.mcp.json`` into *dest* for a single server."""
    data = {"mcpServers": {name: spec}}
    (dest / ".mcp.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _acquire_or_report(source: str, ref: str | None, verb: str) -> AcquiredSource | None:
    """`acquire_source` with the failure already printed (git failure or a
    dead local-path source); None means the caller should return 1."""
    from ._pkg_source import acquire_source

    try:
        return acquire_source(source, ref)
    except (subprocess.CalledProcessError, SourcePathNotFoundError) as e:
        detail = e.stderr.strip() if isinstance(e, subprocess.CalledProcessError) else str(e)
        print(f"[ava mcp {verb}] {detail}", file=sys.stderr)
        return None


def cmd_mcp_install(
    source: str, ref: str | None, path: str | None, env_pairs: list[str] | None = None
) -> int:
    """`ava mcp install <source> [--path SUBDIR] [--ref REF] [--env K=V]...` —
    install a standalone MCP package from a git URL or a local directory.

    The package may ship its own `.mcp.json`; when it does not, the command
    auto-detects the server configuration from `pyproject.toml` (server name,
    module to run, description) and generates `.mcp.json` in the installed copy.

    `--env` injects machine-local values (a bot token, an API key) into the
    landed copy's `env`; the package's own `.mcp.json` ships no secrets.
    """
    from shared import install_registry, paths

    from ._pkg_source import cleanup_temp

    try:
        overrides = _parse_env_pairs(env_pairs or [])
    except ValueError as e:
        print(f"[ava mcp install] {e}", file=sys.stderr)
        return 1

    acq = _acquire_or_report(source, ref, "install")
    if acq is None:
        return 1

    try:
        pkg_dir = acq.root / path if path else acq.root
        if not pkg_dir.is_dir():
            print(f"[ava mcp install] path '{path}' not found in source.", file=sys.stderr)
            return 1
        mcp_json = pkg_dir / ".mcp.json"
        if mcp_json.is_file():
            try:
                name = _package_server_name(mcp_json)
            except ValueError as e:
                print(f"[ava mcp install] {e}", file=sys.stderr)
                return 1
            auto_spec = None
        else:
            try:
                name, auto_spec = _auto_detect_mcp(pkg_dir)
            except ValueError as e:
                print(f"[ava mcp install] {e}", file=sys.stderr)
                return 1

        if name in _builtin_server_names():
            print(
                f"[ava mcp install] '{name}' collides with a built-in MCP server; "
                "rename the server (in the package's .mcp.json or pyproject.toml [project] name).",
                file=sys.stderr,
            )
            return 1
        dest = paths.mcps_dir() / name
        if install_registry.get(name) is not None or dest.exists():
            print(
                f"[ava mcp install] '{name}' already installed; use `ava mcp upgrade {name}`.",
                file=sys.stderr,
            )
            return 1

        from ._manifest_gate import gate_refuses

        if gate_refuses(pkg_dir, command="mcp install", mirror_pyproject=True):
            return 1

        _land_package(acq.cloned, pkg_dir, dest)
        if auto_spec is not None:
            _write_mcp_json(dest, name, auto_spec)
        _apply_env_overrides(dest, name, overrides)
        try:
            _uv_sync(dest)
        except subprocess.CalledProcessError as e:
            shutil.rmtree(dest, ignore_errors=True)
            print(f"[ava mcp install] uv sync failed: {e.stderr.strip()}", file=sys.stderr)
            return 1

        _register_mcp(name, source, path, ref, content_hash=install_registry.tree_hash(dest))
        print(f"[ava mcp install] installed MCP server '{name}' -> {dest}")
        if overrides:
            print(f"  env set on the installed copy: {', '.join(sorted(overrides))}")
        print("  connects on next use; no restart needed.")
        return 0
    finally:
        cleanup_temp(acq.cloned)


def cmd_mcp_uninstall(name: str) -> int:
    """`ava mcp uninstall <name>` — remove an installed MCP package + its entry."""
    from shared import install_registry, paths

    pkg = install_registry.get(name)
    if pkg is None or pkg.type != "mcp":
        print(
            f"[ava mcp uninstall] '{name}' is not an installed MCP package "
            "(built-in / plugin / machine servers are managed elsewhere).",
            file=sys.stderr,
        )
        return 1
    dest = paths.mcps_dir() / name
    if dest.exists():
        shutil.rmtree(dest)
    install_registry.deregister(name)
    print(f"[ava mcp uninstall] removed MCP server '{name}'.")
    return 0


def cmd_mcp_upgrade(name: str, *, force: bool = False) -> int:
    """`ava mcp upgrade <name> [--force]` — re-fetch an installed MCP package
    from its source.

    A locally edited copy (content differs from what the last install/upgrade
    wrote) aborts with a conflict unless `--force` is given — the R5 conflict
    contract, mirroring `git pull` (force = reset --hard).
    """
    from shared import install_registry, paths

    from ._pkg_source import cleanup_temp

    pkg = install_registry.get(name)
    if pkg is None or pkg.type != "mcp":
        print(f"[ava mcp upgrade] '{name}' is not an installed MCP package.", file=sys.stderr)
        return 1
    if pkg.source is None:
        print(f"[ava mcp upgrade] '{name}' has no recorded source.", file=sys.stderr)
        return 1

    dest = paths.mcps_dir() / name
    if dest.exists() and not force and install_registry.copy_changed(dest, pkg.installed_hash):
        print(
            f"[ava mcp upgrade] '{name}' was modified locally; refusing to overwrite. "
            f"Re-run with --force to replace your changes with the fetched source.",
            file=sys.stderr,
        )
        return 1

    acq = _acquire_or_report(pkg.source, pkg.ref, "upgrade")
    if acq is None:
        return 1

    try:
        pkg_dir = acq.root / pkg.path if pkg.path else acq.root
        mcp_json = pkg_dir / ".mcp.json"
        if not mcp_json.is_file():
            print(
                "[ava mcp upgrade] source no longer has .mcp.json at the package root; aborting.",
                file=sys.stderr,
            )
            return 1
        from ava._mcp_config import MCPError, _read_servers

        try:
            servers = _read_servers(mcp_json)
        except MCPError as e:
            print(f"[ava mcp upgrade] bad .mcp.json: {e}", file=sys.stderr)
            return 1
        if name not in servers:
            print(
                f"[ava mcp upgrade] source .mcp.json no longer declares server '{name}'; aborting.",
                file=sys.stderr,
            )
            return 1

        from ._manifest_gate import gate_refuses

        if gate_refuses(pkg_dir, command="mcp upgrade", mirror_pyproject=True):
            return 1

        # Carry the installed copy's env across the re-land: it holds the
        # machine-local values injected at install time (`--env`), which the
        # source deliberately does not ship. Re-applied over the new source env,
        # so an upgrade never silently drops a server's token.
        carried = _landed_env(dest, name)
        _land_package(acq.cloned, pkg_dir, dest)
        _apply_env_overrides(dest, name, carried)
        try:
            _uv_sync(dest)
        except subprocess.CalledProcessError as e:
            print(f"[ava mcp upgrade] uv sync failed: {e.stderr.strip()}", file=sys.stderr)
            return 1

        from datetime import UTC, datetime

        pkg.updated_at = datetime.now(UTC).isoformat(timespec="seconds")
        pkg.installed_hash = install_registry.tree_hash(dest)
        install_registry.register(pkg)
        print(f"[ava mcp upgrade] re-fetched '{name}' from {pkg.source}.")
        print("  connects on next use; no restart needed.")
        return 0
    finally:
        cleanup_temp(acq.cloned)
