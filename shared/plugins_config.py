"""Plugin enable config — per-machine local file `~/.ava/plugins_config.json`.

Different concept from `shared/config.py` (env-driven Settings): this
controls "which plugins are imported and registered at process
startup"; editable via `ava plugins enable/disable`.

Unified plugin concept:
- Only "plugins"; no "builtin hook" / "external plugin" distinction
- Builtin plugins in `<repo>/ava_builtins/plugins/`, external plugins in
  `~/.ava/plugins/`
- Config stores one flat `plugins` dict: {name: {enabled: bool}}
- Builtin/external distinction lives on the filesystem layer, not
  in config

Decentralized-install: enable config lives entirely in the per-machine
`plugins_config.json`; `set_local_enabled` is the only writer.
`load()` reads the local file via `_read_raw()`; it is pure-read
(no write-back). `write_local` is the underlying file writer.
"""

import importlib.util
import inspect
import json
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ValidationError

from shared import paths, skill_names
from shared.plugin_config_registry import merge_disk_image_schema


class PluginEntry(BaseModel):
    """Enable state of a single plugin."""

    enabled: bool


class PluginsConfig(BaseModel):
    """Plugin config — flat dict; all plugins treated equally."""

    plugins: dict[str, PluginEntry] = {}


# Error hierarchy


class PluginsConfigError(Exception):
    """Root of plugins.json read or validation failures."""


class SchemaInvalid(PluginsConfigError):  # noqa: N818
    """JSON is valid but does not match the PluginsConfig schema (Pydantic validation failed)."""


class DanglingPlugin(PluginsConfigError):  # noqa: N818
    """A plugin name referenced by config does not exist on the filesystem.

    ``names`` carries the offending names (the message renders them too), so
    a fail-soft caller can report each one without parsing the message.
    """

    def __init__(self, message: str, *, names: set[str] | None = None) -> None:
        super().__init__(message)
        self.names: set[str] = names or set()


class DuplicatePlugin(PluginsConfigError):  # noqa: N818
    """Same plugin name exists in both builtin and external directories."""


def _discover_plugins() -> dict[str, Path]:
    """Scan builtin + external plugin directories; return {name: plugin_dir}.

    Builtin is scanned first; external plugin matching a builtin name
    -> DuplicatePlugin.

    Dot-prefixed directories (`.name.staging`, `.name.backup-<pid>` — the
    atomic-install residue a hard kill can leave behind, 2026-08-28
    ava_ledger defense line) are skipped: they are transient by name and must
    never surface as ghost plugins.
    """
    discovered: dict[str, Path] = {}

    # Builtin plugin: <repo>/ava_builtins/plugins/<name>/plugin.py
    repo_dir = paths.repo_plugins_dir()
    if repo_dir.exists():
        for p in sorted(repo_dir.iterdir()):
            if p.name.startswith("."):
                continue
            if p.is_dir() and (p / "plugin.py").exists():
                discovered[p.name] = p

    # External plugin: ~/.ava/plugins/<name>/plugin.py
    user_dir = paths.plugins_dir()
    if user_dir.exists():
        for p in sorted(user_dir.iterdir()):
            if p.name.startswith("."):
                continue
            if p.is_dir() and (p / "plugin.py").exists():
                if p.name in discovered:
                    raise DuplicatePlugin(
                        f"plugin '{p.name}' exists in both builtin ({discovered[p.name]}) "
                        f"and external ({p}); same name not allowed. Delete or rename one."
                    )
                discovered[p.name] = p

    return discovered


def parse_description(source_path: Path) -> str:
    """AST-parse the module file; extract `__description__ = "..."` or
    `__description__: str = "..."` value; fall back to first line of
    the module docstring.

    Raises:
        OSError: file unreadable.
        SyntaxError: module file Python syntax invalid.
    """
    import ast

    tree = ast.parse(source_path.read_text())

    fallback = ""
    docstring = ast.get_docstring(tree)
    if docstring:
        fallback = docstring.strip().split("\n", 1)[0].strip()

    def _is_description_string(value: ast.expr) -> str | None:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        return None

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__description__":
                    s = _is_description_string(node.value)
                    if s is not None:
                        return s
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__description__"
            and node.value is not None
        ):
            s = _is_description_string(node.value)
            if s is not None:
                return s
    return fallback


# Core API


def _validate_schema(data: dict[str, Any]) -> PluginsConfig:
    try:
        return PluginsConfig.model_validate(data)
    except ValidationError as e:
        raise SchemaInvalid(f"plugins.json schema invalid: {e}") from e


def _default_config(known_plugins: set[str]) -> PluginsConfig:
    return PluginsConfig(
        plugins={name: PluginEntry(enabled=True) for name in sorted(known_plugins)}
    )


def local_config_path() -> Path:
    """Per-machine plugin-config file path: `~/.ava/plugins_config.json`."""
    return paths.ava_home() / "plugins_config.json"


def _read_local() -> dict[str, Any] | None:
    """Read the per-machine plugin-config file. None = absent; a present file
    that is unreadable, not valid JSON, or not an object raises (fail-fast).

    The same rule `install_registry.load` applies to installed.json: a corrupt
    local config must not silently degrade to the all-enabled default and get
    rewritten from nothing — the corruption is a state-loss signal, not a
    no-op."""
    p = local_config_path()
    if not p.exists():
        return None
    raw = p.read_text()  # OSError propagates
    data = json.loads(raw if raw.strip() else "{}")  # JSONDecodeError propagates
    if not isinstance(data, dict):
        raise PluginsConfigError(f"{p}: expected a JSON object, got {type(data).__name__}")
    return cast("dict[str, Any]", data)


def write_local(raw: dict[str, Any]) -> None:
    """Full-replace the per-machine plugin-config file. The write path for
    `set_local_enabled` (and `ava plugins enable/disable`)."""
    local_config_path().write_text(json.dumps(raw, indent=2))


def _read_raw() -> dict[str, Any]:
    """Read the per-machine local file; absent -> {} (a malformed file raises)."""
    return _read_local() or {}


def load(known_plugins: set[str], *, allow_dangling: bool = False) -> PluginsConfig:
    """Read per-machine plugin config; auto-merge new plugins in memory, return config.

    Pure-read — no write-back. The only writer is `set_local_enabled`.

    Flow:
    1. Local file absent / empty -> default all known plugins enabled=true
    2. Malformed JSON / non-object file -> fail-fast (JSONDecodeError /
       PluginsConfigError), like `install_registry.load` — never a silent
       all-enabled fallback
    3. Pydantic validation failure -> fail-fast (SchemaInvalid)
    4. Validation: plugins referenced by config must exist on local
       filesystem (DanglingPlugin) — unless `allow_dangling=True`, which
       drops the dangling entries instead (treated as disabled; the caller
       is the runtime loader, whose fail-soft contract forbids a config
       mismatch from blocking `import ava`)
    5. Auto-merge: known plugins not in config -> add enabled=true
       in memory (not persisted)

    Args:
        known_plugins: set of plugin names existing on the filesystem.
        allow_dangling: drop config entries whose plugin is not on disk
            instead of raising DanglingPlugin.
    """
    raw = _read_raw()

    if not raw or not raw.get("plugins"):
        return _default_config(known_plugins)

    cfg = _validate_schema(raw)

    # Fold each config key onto the plugin's real directory spelling. A plugin
    # directory has to stay a Python package (`ava_builtins.plugins.<name>` is
    # imported by path), so underscore is the on-disk identity while dash is the
    # name everywhere a human writes it — a hand-edited `plugins_config.json`
    # saying `ava-code` addresses the `ava_code` plugin.
    cfg.plugins = {
        (skill_names.find(name, known_plugins) or name): entry
        for name, entry in cfg.plugins.items()
    }

    # Validate dangling
    dangling = set(cfg.plugins) - known_plugins
    if dangling:
        if not allow_dangling:
            raise DanglingPlugin(
                f"plugins config references non-existent plugins: {sorted(dangling)} "
                f"(known: {sorted(known_plugins) or '<none>'})",
                names=dangling,
            )
        cfg.plugins = {name: entry for name, entry in cfg.plugins.items() if name not in dangling}

    # Auto-merge new plugins (in memory only)
    new_plugins = known_plugins - set(cfg.plugins)
    for name in sorted(new_plugins):
        cfg.plugins[name] = PluginEntry(enabled=True)

    return cfg


def installed_plugin_dirs() -> dict[str, Path]:
    """{name: plugin_dir} for every plugin PRESENT on this machine (builtin +
    external), regardless of enable-state.

    A thin public alias for `_discover_plugins()`. The service roster
    (`ops.spec._plugin_services`) uses this — presence, NOT the agent-facing
    enable-state (`ava plugins enable/disable`) — to fold plugin-declared
    ServiceSpecs into `build_services()`. The roster is a machine/cluster concern
    and must not depend on the agent-plugin-registration plane; a plugin gates its
    own service via an explicit settings field in `ServiceSpec.gate` (e.g.
    task-maintenance's `AVA_TASK_MAINTENANCE_ENABLED`).
    """
    return _discover_plugins()


def set_local_enabled(name: str, *, enabled: bool) -> PluginsConfig:
    """Flip one plugin's enabled flag in the per-machine local config file.

    Reads the current local config, writes a config scoped to the plugins
    present on THIS machine to `plugins_config.json`. Scoping to local
    plugins is what keeps a later load() from raising DanglingPlugin.

    Raises:
        DanglingPlugin: `name` is not a plugin installed on this machine.
    """
    known = set(_discover_plugins())
    resolved = skill_names.find(name, known)
    if resolved is None:
        raise DanglingPlugin(
            f"plugin {name!r} is not present on this machine (known: {sorted(known) or '<none>'})"
        )
    name = resolved
    raw = _read_raw()
    existing = _validate_schema(raw).plugins if raw.get("plugins") else {}
    # Sorted so the written plugins.json key order (and thus the on-disk
    # iteration order that drives hook registration order) is deterministic
    # rather than set-hash-dependent.
    plugins = {n: existing.get(n, PluginEntry(enabled=True)) for n in sorted(known)}
    plugins[name] = PluginEntry(enabled=enabled)
    cfg = PluginsConfig(plugins=plugins)
    write_local(cfg.model_dump())
    return cfg


class PluginUpdateEntry(BaseModel):
    """Result for a single plugin after disk-image schema merge."""

    name: str
    status: Literal["updated", "no_diff", "skipped", "error"]
    added: list[str] = []
    removed: list[str] = []
    detail: str | None = None  # reason for skipped; exception message for error


class PluginUpdateResult(BaseModel):
    """Response body of `update_all_disk_images()`."""

    entries: list[PluginUpdateEntry]


def update_all_disk_images() -> PluginUpdateResult:
    """Scan all plugins, auto-merge disk-image schema diff, return structured result.

    Driven by the `ava plugins update` CLI (`cli/commands/plugins.py`); the
    `ava start` converge step runs the same path. There is no gateway endpoint
    for it. Each plugin goes through
    `shared.plugin_config_registry.merge_disk_image_schema(name, Cls)`:
      - disk image missing -> write default
      - new field -> fill into disk image with cls default
      - removed field -> dropped from the disk image (so the image converges to
        the current schema and agent spawns stop hitting SchemaDriftError)
      - type incompatible -> that entry status='error'; does not
        interrupt other plugins

    Only imports each plugin's `default_config.py` (not plugin.py)
    — to avoid triggering hook registration / state registration
    side effects.
    """
    entries: list[PluginUpdateEntry] = []
    for name, plugin_dir in sorted(_discover_plugins().items()):
        default_config_py = plugin_dir / "default_config.py"
        if not default_config_py.exists():
            entries.append(
                PluginUpdateEntry(name=name, status="skipped", detail="no default_config.py")
            )
            continue

        spec = importlib.util.spec_from_file_location(
            f"plugins.{name}.default_config", default_config_py
        )
        if spec is None or spec.loader is None:
            entries.append(
                PluginUpdateEntry(
                    name=name, status="error", detail="spec_from_file_location returned None"
                )
            )
            continue
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            entries.append(PluginUpdateEntry(name=name, status="error", detail=str(e)))
            continue

        cls_candidates = [
            obj
            for _attr, obj in inspect.getmembers(module)
            if inspect.isclass(obj) and issubclass(obj, BaseModel) and obj is not BaseModel
        ]
        if not cls_candidates:
            entries.append(
                PluginUpdateEntry(
                    name=name, status="error", detail="default_config.py has no BaseModel subclass"
                )
            )
            continue
        if len(cls_candidates) > 1:
            entries.append(
                PluginUpdateEntry(
                    name=name,
                    status="error",
                    detail=(
                        f"default_config.py has {len(cls_candidates)} BaseModel subclasses, "
                        f"want exactly 1: {[c.__name__ for c in cls_candidates]}"
                    ),
                )
            )
            continue
        cls = cls_candidates[0]

        try:
            added, removed = merge_disk_image_schema(name, cls)
        except Exception as e:
            entries.append(PluginUpdateEntry(name=name, status="error", detail=str(e)))
            continue

        if not added and not removed:
            entries.append(PluginUpdateEntry(name=name, status="no_diff"))
        else:
            entries.append(
                PluginUpdateEntry(
                    name=name, status="updated", added=sorted(added), removed=sorted(removed)
                )
            )

    return PluginUpdateResult(entries=entries)
