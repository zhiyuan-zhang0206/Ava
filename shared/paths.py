"""$AVA_HOME path resolution — single-point entry shared by gateway /
agent / subprocess.

`shared/config.py:Settings.ava_home` is the path source; this module
adds first-access mkdir side effects, so calling a helper means "the
directory is ready and writable". pydantic-settings reads the
$AVA_HOME env var at import time; the three process classes inherit
the same variable, no manual passing required.
"""

from pathlib import Path

from shared.config import settings
from shared.private_storage import ensure_private_dir


def ava_home() -> Path:
    """User data root directory; create if missing."""
    root = Path(settings.general.ava_home).expanduser()
    return ensure_private_dir(root)


def run_dir() -> Path:
    """Runtime directory for pid/sock files ($AVA_HOME/run); create if missing.

    Keeps per-unit ephemeral runtime artifacts (pidfiles and unix sockets)
    under one subdirectory so the $AVA_HOME top level stays lean."""
    target = ava_home() / "run"
    target.mkdir(parents=True, exist_ok=True)
    return target


def pid_path(service_name: str) -> Path:
    """Pidfile path for a named service ($AVA_HOME/run/<service>.pid).

    The single source of truth for pidfile naming — all daemons write their
    pid through this function so a future move is one-line."""
    return run_dir() / f"{service_name}.pid"


def legacy_pid_path(service_name: str) -> Path:
    """Old pidfile location ($AVA_HOME/<service>.pid) — kept for backward
    compat during the transition to run/ subdirectory. Callers check this
    path first when probing for an already-running instance, then write new
    pidfiles to the path returned by pid_path()."""
    return ava_home() / f"{service_name}.pid"


def plugins_config_path() -> Path:
    """plugins.json path — returns the path only; load() decides whether to write defaults."""
    return ava_home() / "plugins.json"


def install_registry_path() -> Path:
    """Install registry file ($AVA_HOME/installed.json) — records externally
    installed packages and their enable state. Returns the path only; the
    registry module decides read/write."""
    return ava_home() / "installed.json"


def plugins_dir() -> Path:
    """External plugin directory (~/.ava/plugins/); create if missing."""
    target = ava_home() / "plugins"
    target.mkdir(parents=True, exist_ok=True)
    return target


def repo_root() -> Path:
    """Repository root, derived from this module's location (`shared/paths.py`)."""
    return Path(__file__).resolve().parent.parent


def prod_service_checkout_error(repo: Path) -> str | None:
    """Why `repo` must not launch services for this unit, or None when it may.

    The 01:13 worktree accident (Task #966): `repo_root()` is "whoever imported
    shared/paths.py", and a dev clone or worktree with no `.ava_home` pointer
    resolves as an UNANCHORED checkout (dotenv_boot rule 4) to the prod
    default home `~/.ava`. `ava start` from such a checkout then binds every
    prod daemon to that checkout's code, and deleting the checkout — routine
    worktree cleanup — removes the floor under the running fleet. The prod
    home's services may only be launched from its own anchored checkout
    (`~/.ava/source`); every other checkout is refused.

    A non-prod unit (any home other than the default `~/.ava`) always passes:
    a dev cluster's own home is anchored to its own checkout, and running that
    checkout's code is exactly what its home is for.
    """
    # Both sides resolved: a non-canonical AVA_HOME spelling that resolves to the
    # prod home (`$HOME/../.ava`, a symlinked path) must not bypass the refusal —
    # the filesystem would land the writes in the same directory either way
    # (same-class hardening as `_assert_env_agrees_with_checkout`'s resolve
    # comparison).
    home = ava_home().resolve()
    if home != (Path.home() / ".ava").resolve():
        return None
    expected = Path.home() / ".ava" / "source"
    if repo.resolve() == expected.resolve():
        return None
    return (
        f"this unit is the prod home ({home}) but the launching checkout is {repo} "
        f"— only the prod home's anchored checkout ({expected}) may launch its "
        f"services; a dev checkout would bind every prod daemon to disposable code "
        f"(01:13 worktree accident, Task #966). Run `ava start` from {expected}."
    )


def repo_plugins_dir() -> Path:
    """Built-in plugin directory (<repo>/ava_builtins/plugins/). Derived from this module's path."""
    return repo_root() / "ava_builtins" / "plugins"


def logs_dir() -> Path:
    """Per-unit log directory ($AVA_HOME/logs); create if missing."""
    target = ava_home() / "logs"
    target.mkdir(parents=True, exist_ok=True)
    return target


def chrome_mcp_socket() -> Path:
    """Unix socket of the per-machine shared chrome MCP service
    ($AVA_HOME/run/chrome-mcp.<cdp_port>.sock). Keyed by the CDP port so
    co-hosted clusters (each with its own browser_cdp_port) get distinct
    sockets; the per-agent chrome bridge dials this, the daemon listens on it."""
    return run_dir() / f"chrome-mcp.{settings.services.browser_cdp_port}.sock"


def permissions_helper_socket() -> Path:
    """Unix socket of this cluster's macOS permissions helper daemon
    ($AVA_HOME/run/permissions-helper.<port>.sock). Keyed by the per-cluster
    helper port so co-hosted clusters get distinct sockets; the per-agent client
    dials this, the signed launchd helper listens on it. (The TCC grant the
    helper relies on is shared machine-wide via a fixed bundle id, but the
    process and socket are per-cluster.)"""
    return run_dir() / f"permissions-helper.{settings.services.permissions_helper_port}.sock"


def traces_dir() -> Path:
    """Per-unit trace mirror directory ($AVA_HOME/traces); create if missing.

    Holds the local OTLP-JSON span mirror written by the OTel Collector
    sidecar's file exporter (task #1266): the active `spans.jsonl` plus
    rotated `spans-<ISO-timestamp>.jsonl` backups, each line one
    `ExportTraceServiceRequest`. `ava trace ship` replays a window of it
    straight to Tempo when the live fan-out missed data."""
    target = ava_home() / "traces"
    target.mkdir(parents=True, exist_ok=True)
    return target


def otel_collector_dir() -> Path:
    """Per-unit OTel Collector sidecar root ($AVA_HOME/otel-collector); create
    if missing.

    Owns the pinned otelcol-contrib binary, the generated config.yaml (from
    the repo template, baked at converge) and the file_storage persistent
    queue directory. One sidecar per machine — see
    `cli/commands/_converge.py:_ensure_otel_collector_step`."""
    target = ava_home() / "otel-collector"
    target.mkdir(parents=True, exist_ok=True)
    return target


def otel_collector_binary() -> Path:
    """Path of the pinned otelcol-contrib binary (platform-appropriate name)."""
    from shared.platform import IS_WINDOWS
    from shared.runtime_interpreter import WHEEL_RUNTIME, runtime_otel_binary

    if WHEEL_RUNTIME:
        return runtime_otel_binary()

    return otel_collector_dir() / ("otelcol-contrib.exe" if IS_WINDOWS else "otelcol-contrib")


def otel_collector_config() -> Path:
    """Path of the generated sidecar config.yaml (baked at converge)."""
    return otel_collector_dir() / "config.yaml"


def memory_dir() -> Path:
    """Per-unit memory git repo root ($AVA_HOME/memory). Returns the path only;
    the caller (memory_repo) runs git init / clone.

    On a unit that also carries 'gateway' capability, this is the agent-runner's
    authoring checkout (machine-<name> branch); the gateway indexes from a
    separate consolidated checkout returned by gateway_memory_dir()."""
    return ava_home() / "memory"


def gateway_memory_dir() -> Path:
    """Gateway's consolidated memory checkout path. On a combined unit
    (gateway+agent-runner on one host) this is $AVA_HOME/gateway/memory to
    keep the gateway's index source (main branch) separate from the
    agent-runner's authoring checkout (machine-<name> branch at memory_dir()).
    On a gateway-only unit it is the same as memory_dir(). Returns the path
    only."""
    from shared.machine import is_agent_runner

    if is_agent_runner():
        return ava_home() / "gateway" / "memory"
    return memory_dir()


def skills_dir() -> Path:
    """Per-unit skill load dir ($AVA_HOME/skills) — the single directory the
    skill scanner reads; repo + plugin skills are converged into it."""
    return ava_home() / "skills"


def mcps_dir() -> Path:
    """Per-unit installed-MCP load dir ($AVA_HOME/mcps) — where `ava mcp install`
    lands each installed MCP package (`<name>/` holding its own `.mcp.json` +
    `pyproject.toml` + `.venv`). The MCP config loader scans each `<name>/.mcp.json`
    here as its installed layer, gated by the install registry; built-in MCPs stay in
    the repo's `<repo>/ava_builtins/mcps/`. Symmetric with `skills_dir()` / `plugins_dir()`."""
    return ava_home() / "mcps"


def workspace_dir(agent_id: int) -> Path:
    """Per-agent workspace ($AVA_HOME/workspaces/<agent_id>); create if missing.

    A stable scratch area for files an agent produces or downloads. Survives
    restarts and termination, is not git-managed, and is never garbage-collected
    by the framework — cleanup is an ops decision. Cross-agent sharing happens
    by passing absolute paths in messages, not by writing into each other's
    workspace."""
    target = ava_home() / "workspaces" / str(agent_id)
    target.mkdir(parents=True, exist_ok=True)
    return target


def exec_run_dir() -> Path:
    """Per-unit exec-subprocess scratch dir ($AVA_HOME/run/exec) — request /
    result envelopes for one execute_code run each (agent/graph/_exec_protocol.py),
    pruned per agent subdir. Create if missing."""
    target = run_dir() / "exec"
    target.mkdir(parents=True, exist_ok=True)
    return target


def monitors_dir() -> Path:
    """Per-unit monitor-script directory ($AVA_HOME/monitors)."""
    return ava_home() / "monitors"


def disabled_services_file() -> Path:
    """Per-unit record of the services an operator durably disabled via
    `ava start --disable-service`. Written by start (operator intent) and read by
    the watchdog so a disabled service stays down instead of being revived on the
    next 60s healthcheck round. Absent file = nothing disabled.

    Returns the path only; `shared.disabled_services` owns read/write.
    """
    return ava_home() / "disabled_services"


def launch_failures_path() -> Path:
    """Path of the `$AVA_HOME/last_launch_failures` file — the session names the
    last `ava start` on this host could not launch.

    Exists because the rollout's local leg runs `ava start` in a FRESH child
    process (`update._boot_gateway_fresh`), and an exit code cannot carry names.
    The parent orchestration needs the names to put them in the ROLLOUT
    aftermath block, and it cannot compute them itself: its own interpreter is
    the pre-pull one, so its service roster is the old tree's. The child has the
    new tree's roster and writes what it actually failed to launch.

    Written by every `ava start` (an empty failure set unlinks it), so a read is
    never stale; taken — read then unlinked — by whoever consumes it.

    Returns the path only; ``shared.launch_failures`` owns read/write.
    """
    return ava_home() / "last_launch_failures"


def running_sha_path() -> Path:
    """Path of the `$AVA_HOME/running_sha` file that records the commit the
    gateway was last started on.

    Written by `ava start` (on every host, regardless of role) right before
    launching services; read by the update change-detection path so a manual
    `git pull` between start and update cannot hide code changes (the diff is
    against the running commit, not the current HEAD). Absent file = first-ever
    start or uninitialised; the reader falls back to HEAD.

    Returns the path only; ``shared.running_sha`` owns read/write.
    """
    return ava_home() / "running_sha"


def installed_sha_path() -> Path:
    """Path of the `$AVA_HOME/installed_sha` file that records the commit this
    host last fully installed (checked out + uv sync + migrations applied).

    Written by `ava cluster update` after a successful checkout + sync + migrate, and
    by `ava start` right after it applies pending migrations. Read by the
    source-integrity guard at `ava start` to detect manual git operations in
    the source tree that bypass the update flow.

    Returns the path only; ``shared.source_integrity`` owns read/write.
    """
    return ava_home() / "installed_sha"


def computer_mcp_socket() -> Path:
    """Unix socket of the per-machine shared computer MCP service
    ($AVA_HOME/run/computer-mcp.sock). The per-agent bridge / the MCP daemon's
    direct dial connect here; the computer-mcp daemon listens on it. One
    service per machine (the desktop is one shared screen), so no port key."""
    return run_dir() / "computer-mcp.sock"


def mcp_daemon_shared_socket() -> str:
    """Filesystem path of the per-machine shared MCP daemon socket — one
    socket under `$AVA_HOME/run` serving every agent on the machine (the daemon
    isolates sessions per client connection; see ava/_mcps_daemon.py). The
    single source of truth for the naming convention: the daemon binds here and
    the client (ava.mcps) connects here. Lives in shared/ so the agent kernel
    can import it without going through the agent-facing `ava.mcps`, which
    AVA_SDK_DISABLE may replace with a stub. Run as an agent-runner service
    (ops roster session "mcp-daemon"), watchdog-managed like browser-mcp."""
    return str(run_dir() / "mcp_daemon.sock")
