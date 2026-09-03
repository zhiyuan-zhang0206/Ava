"""Shared MCP server config loader.

Both the in-process tool surface (`ava.mcps`) and the long-lived connection
process read MCP server definitions through this one loader, so they always
agree on the same merged server set.

The merged map has four layers, applied in this order (later wins on a name
collision):

1. built-in — `<repo>/ava_builtins/mcps/<name>/.mcp.json`, the servers that ship with the
   repo (currently just `chrome`).
2. plugin-bundled — a plugin ships its own server defaults at its root
   (built-in `<repo>/ava_builtins/plugins/<name>/.mcp.json`, then installed
   `$AVA_HOME/plugins/<name>/.mcp.json`).
3. installed — `$AVA_HOME/mcps/<name>/.mcp.json`, MCP packages added via
   `ava mcp install`, each a self-contained dir gated by the install registry
   (`shared.install_registry`, `type="mcp"`). An installed server's command is
   a relative `.venv/bin/python …` resolved against that dir (see
   `installed_mcp_dir`), so its deps stay isolated from core.
4. machine — `$AVA_HOME/mcp.json`, applied last, so a hand-added machine entry
   overrides any same-named default.

Each file declares servers under an `mcpServers` object (Claude Code's layout);
a file lacking that section contributes nothing. A per-host enable overlay
(`mcp_enabled.json`) then drops disabled servers, uniformly across all layers.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any, TypedDict

from shared.paths import ava_home, mcps_dir, repo_root
from shared.platform import IS_WINDOWS
from shared.platform_probes import display_available, unix_sockets_available
from shared.runtime_interpreter import external_plugin_read_root


class MCPError(Exception):
    """Base class for `ava.mcps` failures."""


class MCPServerNotFound(MCPError):  # noqa: N818 — same naming convention as FileNotFoundError
    pass


class MCPConnectError(MCPError):
    pass


class MCPToolNotFound(MCPError):  # noqa: N818
    pass


class MCPCallError(MCPError):
    pass


class ToolInfo(TypedDict):
    name: str
    description: str
    input_schema: dict[str, Any]


def assert_requirements(spec: dict[str, Any]) -> None:
    """Evaluate a server entry's optional `requires` preconditions before connect.

    Raises MCPError with an actionable message if a precondition is unmet, so the
    agent gets a clear capability error instead of the underlying tool's opaque
    failure (e.g. chrome-devtools-mcp's bare "connection refused" on a host with
    no display). Unknown requirement keys fail fast.

    Two host capabilities are recognized. `display` is what a headed browser
    draws on. `unix_socket` is for a server whose transport is an AF_UNIX socket:
    `chrome` is one — it fronts the `browser-mcp` daemon, which listens with
    `asyncio.start_unix_server` — so on Windows the entry would hand an agent a
    tool that cannot reach a service that cannot run (`ops.spec._gate_reason`
    gates that daemon out there over the same fact).
    """
    requires = spec.get("requires")
    if not requires:
        return
    for key, want in requires.items():
        if key == "display":
            if want and not display_available():
                raise MCPError(
                    "MCP server requires a display, but this host has none "
                    "(headless server / WSL without WSLg)"
                )
        elif key == "unix_socket":
            if want and not unix_sockets_available():
                raise MCPError(
                    "MCP server requires AF_UNIX sockets, which this host has none of "
                    "(Windows) — the service it fronts cannot run here either"
                )
        else:
            raise MCPError(f"unknown requires key {key!r} in MCP server config")


def server_capability(spec: dict[str, Any]) -> tuple[bool, str | None]:
    """Non-raising read-time capability check for a server's `requires`.

    Mirrors assert_requirements but returns (ok, reason) instead of raising, for
    a "can this host enable it?" UI gate. `display` and `unix_socket` are both
    statically checkable here; unknown/other requirement keys are left to the
    connect-time assert_requirements (so this returns ok for them).
    """
    requires = spec.get("requires")
    if not requires:
        return (True, None)
    if requires.get("display") and not display_available():
        return (False, "requires a display, but this host has none")
    if requires.get("unix_socket") and not unix_sockets_available():
        return (False, "requires AF_UNIX sockets, which this host has none of")
    return (True, None)


# The relative interpreter path every `.mcp.json` we own is authored with. It is
# a repo convention, not a platform fact — the file is committed once and read on
# every platform, so the reader maps it onto this host's venv layout (the same
# substitution `shared.session_backend` does for supervised session commands).
_POSIX_VENV_PYTHON = ".venv/bin/python"
_WINDOWS_VENV_PYTHON = ".venv\\Scripts\\python.exe"


def server_url(spec: dict[str, Any]) -> str | None:
    """The server's remote Streamable HTTP endpoint, or None for a stdio server.

    A `url` entry declares a remote server — no child process: the daemon dials
    the endpoint over Streamable HTTP (the 2026-07-28 protocol revision is
    stateless there, so one endpoint serves every agent connection). Static
    headers (API keys) ride in `headers`; OAuth flows are not supported — a
    server that requires one fails fast at connect.

    Validation is fail-fast (fail on unknown shapes, never guess):
    - `url` must be an http(s) string
    - `url` and `command` are mutually exclusive — a server is either local
      stdio or remote HTTP, never both
    - `headers`, when present, must be a dict of str -> str

    Raises:
        MCPError: the entry's url/headers shape is invalid.
    """
    url = spec.get("url")
    if url is None:
        return None
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise MCPError(f"server 'url' must be an http(s) endpoint, got {url!r} (expected a string)")
    if spec.get("command") is not None:
        raise MCPError("server declares both 'url' and 'command' — pick one transport")
    headers = spec.get("headers")
    if headers is not None and not (
        isinstance(headers, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())
    ):
        raise MCPError(f"server 'headers' must be a dict of str -> str, got {headers!r}")
    oauth = spec.get("oauth")
    if oauth is not None and not isinstance(oauth, bool):
        raise MCPError(f"server 'oauth' must be a boolean, got {oauth!r}")
    if oauth and headers:
        raise MCPError("server declares both 'oauth' and 'headers' — pick one auth mode")
    return url


def resolve_command(cmd: str) -> str:
    """Map the repo's `.venv/bin/python` convention onto this host's venv layout.

    Both layers we own (built-in `ava_builtins/mcps/`, installed `$AVA_HOME/mcps/`)
    spawn their server with a relative `.venv/bin/python`, resolved against the
    cwd `server_cwd` pins. That path does not exist on Windows, where a venv puts
    the interpreter at `.venv\\Scripts\\python.exe`. Anything else — a plugin or
    machine entry's own command line — is returned untouched: those are a third
    party's argv and must not be reinterpreted.
    """
    if IS_WINDOWS and cmd == _POSIX_VENV_PYTHON:
        return _WINDOWS_VENV_PYTHON
    return cmd


def _machine_config_path() -> Path:
    """`mcp.json` path — goes through `ava_home()` so AVA_HOME env injection takes effect.

    Avoids a module-level constant: in docker / eval scenarios AVA_HOME may
    change after import (a per-task temporary config directory).
    """
    return ava_home() / "mcp.json"


def _read_servers(path: Path) -> dict[str, dict[str, Any]]:
    """Read the `mcpServers` section of one JSON config file.

    Missing file or missing `mcpServers` section → empty dict (tolerates a
    generic settings file that carries no MCP section).

    Raises:
        MCPError: the file fails to parse, or its `mcpServers` field is present
            but not an object.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise MCPError(f"Failed to read {path}: {type(e).__name__}: {e}") from e
    if not isinstance(data, dict):
        raise MCPError(f"`{path}` is not a JSON object (got {type(data).__name__})")
    section = data.get("mcpServers")
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise MCPError(f"`{path}` mcpServers field is not a dict (got {type(section).__name__})")
    return section


def _builtin_mcp_paths() -> list[Path]:
    """Built-in MCP declarations shipped in the repo's `ava_builtins/mcps/` folder
    (`<repo>/ava_builtins/mcps/*/.mcp.json`), sorted by name. Symmetric with built-in skills
    (`ava_builtins/skills/`) and plugins (`ava_builtins/plugins/`)."""
    root = Path(__file__).resolve().parent.parent / "ava_builtins" / "mcps"
    if not root.is_dir():
        return []
    paths: list[Path] = []
    for entry in sorted(root.iterdir()):
        mcp_json = entry / ".mcp.json"
        if mcp_json.is_file():
            paths.append(mcp_json)
    return paths


def _installed_mcp_paths() -> list[Path]:
    """Installed-MCP `.mcp.json` files under `$AVA_HOME/mcps/<name>/`, one per
    registry-tracked `type="mcp"` package, sorted by name.

    Only registry-tracked dirs contribute — a stray dir left under the load dir
    is ignored (mirrors the skill scanner's registry-gated load). `ava mcp
    install` writes the dir and the registry row together; `ava mcp uninstall`
    removes both.
    """
    from shared.install_registry import installed_mcp_names

    root = mcps_dir()
    if not root.is_dir():
        return []
    paths: list[Path] = []
    for name in sorted(installed_mcp_names()):
        mcp_json = root / name / ".mcp.json"
        if mcp_json.is_file():
            paths.append(mcp_json)
    return paths


def installed_mcp_dir(name: str) -> Path | None:
    """The subprocess cwd for an installed MCP server, or None for a built-in /
    plugin / machine server (which keep the daemon's own cwd, the repo root).

    An installed server's `.mcp.json` command is a relative `.venv/bin/python
    -m <module>`; spawning it with cwd=`$AVA_HOME/mcps/<name>/` resolves that
    relative path to the package's own isolated venv, so its deps never touch
    core. Returns the dir only when the server truly comes from the installed
    layer: it must be registry-tracked, carry a `.mcp.json`, and not be shadowed
    by a same-named machine-config entry (the one layer above installed — that
    entry would be spawned from the daemon cwd instead).
    """
    from shared.install_registry import installed_mcp_names

    if name not in installed_mcp_names():
        return None
    if name in _read_servers(_machine_config_path()):
        return None
    d = mcps_dir() / name
    return d if (d / ".mcp.json").is_file() else None


def server_cwd(name: str) -> Path | None:
    """Working directory for spawning `name`'s server, or None to inherit the
    daemon's own cwd. Resolved by which layer actually provides the server.

    Both layers we own use a **relative** interpreter path in their `.mcp.json`
    (`.venv/bin/python`), never `uv run` — a resident wrapper process per agent,
    which at high agent density is pure overhead. A relative argv[0] is resolved
    against the spawned child's cwd, so pinning cwd is what makes those commands
    portable (no machine-specific absolute path in a committed file):

    - installed (`$AVA_HOME/mcps/<name>/`) → that package dir, so its
      `.venv/bin/python` is the package's own isolated venv.
    - built-in (`<repo>/ava_builtins/mcps/<name>/`) → the repo root, so `.venv/bin/python` is
      the repo venv and `-m <pkg>` finds the repo's top-level packages. Pinning
      it also removes the built-ins' former reliance on whatever cwd the agent
      process happened to have.
    - plugin / machine → None: those entries carry a third party's own command
      line (absolute or PATH-resolved), so we must not reinterpret it.
    """
    # Machine config is the top layer — the user's own command line.
    if name in _read_servers(_machine_config_path()):
        return None
    installed = installed_mcp_dir(name)
    if installed is not None:
        return installed
    for path in _plugin_config_paths():
        if name in _read_servers(path):
            return None
    for path in _builtin_mcp_paths():
        if name in _read_servers(path):
            return repo_root()
    return None


def _plugin_config_paths() -> list[Path]:
    """Plugin-bundled `.mcp.json` files, in apply order.

    Built-in plugins (`<repo>/ava_builtins/plugins/*/.mcp.json`) first, then installed ones
    (retained generation in wheel mode, `$AVA_HOME/plugins` in source mode);
    each group is sorted by plugin name. Release preparation separately refuses
    an undeclared MCP executable closure rather than treating config as proof.
    """
    paths: list[Path] = []
    repo_plugins = Path(__file__).resolve().parent.parent / "ava_builtins" / "plugins"
    for root in (repo_plugins, external_plugin_read_root()):
        if not root.is_dir():
            continue
        for plugin_dir in sorted(root.iterdir()):
            if plugin_dir.name.startswith("."):
                continue  # staging/backup residue — never a real plugin
            mcp_json = plugin_dir / ".mcp.json"
            if mcp_json.is_file():
                paths.append(mcp_json)
    return paths


def load_mcp_config(*, include_disabled: bool = False) -> dict[str, dict[str, Any]]:
    """Merged MCP server map: built-in `ava_builtins/mcps/` < plugin `.mcp.json` < machine config.

    Returns an empty dict when nothing is configured.

    A per-host overlay records individual servers as disabled. By default a
    disabled server is dropped from the returned map (what the running surfaces
    consume). Pass include_disabled=True to get the full merged map with the
    overlay state ignored — for callers that need to show disabled servers and
    cross-reference the overlay themselves. A server absent from the overlay
    stays (default-on).

    Raises:
        MCPError: any source file fails to parse or has a non-dict `mcpServers`.
    """
    merged: dict[str, dict[str, Any]] = {}
    for path in _builtin_mcp_paths():
        merged.update(_read_servers(path))
    for path in _plugin_config_paths():
        merged.update(_read_servers(path))
    for path in _installed_mcp_paths():
        merged.update(_read_servers(path))
    merged.update(_read_servers(_machine_config_path()))
    if include_disabled:
        return merged
    from shared.mcp_enabled import McpEnabledConfigError, read_enabled

    try:
        enabled = read_enabled()
    except McpEnabledConfigError:
        # Fail closed: a corrupt overlay's intent is unknown — do not
        # silently resurrect servers the operator disabled (audit 2026-08-08
        # P2). All-disabled is loud (missing tools), never a security hole.
        return {}
    return {k: v for k, v in merged.items() if enabled.get(k, True)}


@functools.lru_cache(maxsize=1)
def _session_death_codes() -> frozenset[int]:
    """MCP error codes the client SDK synthesizes when a session is unusable:
    CONNECTION_CLOSED when the stdio peer's read loop hit EOF, REQUEST_TIMEOUT
    when a call got no reply. Imported lazily so this module still imports
    where `mcp` is absent."""
    try:
        from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT
    except ImportError:
        return frozenset()
    return frozenset({CONNECTION_CLOSED, REQUEST_TIMEOUT})


def is_transport_error(exc: BaseException) -> bool:
    """True when `exc` means the MCP server process / transport died and a
    reconnect + retry is appropriate — vs a tool-level error that must
    propagate untouched (retrying would double-run side-effectful tools).

    Shared by the MCP daemon and the in-process SDK so both sides agree on
    the retry seam.
    """
    name = type(exc).__name__
    if name in (
        "BrokenResourceError",
        "ClosedResourceError",
        "BrokenPipeError",
        "ConnectionResetError",
        "ConnectionRefusedError",
        "TimeoutError",
    ):
        return True
    # OSError with an explicit transport errno (EPIPE / ECONNRESET / ECONNREFUSED).
    # TimeoutError is a subclass of OSError but has no errno — caught by the
    # name check above, not this branch.
    if isinstance(exc, OSError):
        errno = getattr(exc, "errno", None)
        if errno in (32, 54, 61):  # EPIPE, ECONNRESET, ECONNREFUSED
            return True
    # The mcp SDK raises session-death errors `from None` (no __cause__) with
    # a code — the __cause__ probe alone missed them (2026-08-13 #1229).
    if name in ("McpError", "MCPError", "JSONRPCError"):
        code = getattr(exc, "code", None)
        if code in _session_death_codes():
            return True
        cause = getattr(exc, "__cause__", None)
        if cause is not None:
            return is_transport_error(cause)
    return False
