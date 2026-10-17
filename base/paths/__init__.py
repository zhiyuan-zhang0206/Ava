"""$MEIVA_HOME path resolution — single-point entry shared by gateway /
agent / subprocess.

`base.paths.home.resolve_meiva_home` is the path source (`$MEIVA_HOME`,
else `~/.meiva`, read on every call); `meiva_home()` adds the mkdir side effect for
the home itself. A helper that takes `home` is a pure derivation (no environment,
no mkdir); the zero-argument helpers that remain still create their directory on
first access. The three process classes inherit the same variable.
The package holds only this door; its doc nodes (`docs/paths.meiva.okf.md`,
`docs/lock-discipline.meiva.okf.md`) sit beside it.
"""

from pathlib import Path

from base.paths.home import home_checkout_error, resolve_meiva_home
from base.paths.private_dir import ensure_private_dir


def meiva_home() -> Path:
    """User data root directory; create if missing."""
    return ensure_private_dir(resolve_meiva_home())


def run_dir(home: Path) -> Path:
    """Runtime directory for pid/sock files (`<home>/run`).

    Keeps per-unit ephemeral runtime artifacts (pidfiles and unix sockets)
    under one subdirectory so the home's top level stays lean. Pure: the process
    that binds a socket or writes a pidfile creates the directory."""
    return home / "run"


def install_registry_path(home: Path) -> Path:
    """Install registry file (`<home>/installed.json`) — records externally
    installed packages and their enable state. Pure: the path only; the
    registry module decides read/write."""
    return home / "installed.json"


def plugins_dir() -> Path:
    """External plugin directory (~/.meiva/plugins/); create if missing."""
    target = meiva_home() / "plugins"
    target.mkdir(parents=True, exist_ok=True)
    return target


def repo_root() -> Path:
    """Repository root, derived from this module's location (`base/paths/__init__.py`)."""
    return Path(__file__).resolve().parents[2]


def prod_service_checkout_error(repo: Path) -> str | None:
    """Why `repo` must not launch services for this unit, or None when it may.

    The 01:13 worktree accident (Task #966): `repo_root()` is "whoever imported
    base/paths/__init__.py", and a prod daemon bound to a dev clone's or worktree's
    code loses its floor when that checkout is deleted — routine worktree
    cleanup. A home that carries its own `<home>/source` checkout (the prod home
    `~/.meiva`) may only launch services from that checkout; a home with none (a
    test home) accepts any. The rule is `base.paths.home.home_checkout_error`, shared
    with the CLI's pre-Settings gate.
    """
    return home_checkout_error(repo)


def repo_plugins_dir() -> Path:
    """Built-in plugin directory (<repo>/meiva_builtins/plugins/). Derived from this module's path."""
    return repo_root() / "meiva_builtins" / "plugins"


def logs_dir() -> Path:
    """Per-unit log directory ($MEIVA_HOME/logs); create if missing."""
    target = meiva_home() / "logs"
    target.mkdir(parents=True, exist_ok=True)
    return target


def chrome_mcp_socket(home: Path, cdp_port: int) -> Path:
    """Unix socket of the per-machine shared chrome MCP service
    (`<home>/run/chrome-mcp.<cdp_port>.sock`). Keyed by the CDP port so
    co-hosted clusters (each with its own browser_cdp_port) get distinct
    sockets; the per-agent chrome bridge dials this, the daemon listens on it."""
    return run_dir(home) / f"chrome-mcp.{cdp_port}.sock"


def permissions_helper_socket(home: Path, port: int) -> Path:
    """Unix socket of this cluster's macOS permissions helper daemon
    (`<home>/run/permissions-helper.<port>.sock`). Keyed by the per-cluster
    helper port so co-hosted clusters get distinct sockets; the per-agent client
    dials this, the signed launchd helper listens on it. (The TCC grant the
    helper relies on is shared machine-wide via a fixed bundle id, but the
    process and socket are per-cluster.)"""
    return run_dir(home) / f"permissions-helper.{port}.sock"


def permissions_helper_app_dir() -> Path:
    """Stable install directory for the macOS permissions-helper app.

    The app lives outside the source checkout so a release checkout cannot
    replace it or change its freshness metadata beneath a running helper.
    """
    return meiva_home() / "helper"


def traces_dir(home: Path) -> Path:
    """The trace mirror directory under a unit home (`<home>/traces`). Pure: reads no
    environment and creates nothing; the caller owns the home and ensures the directory.

    Holds the local OTLP-JSON span mirror written by the OTel Collector
    sidecar's file exporter (task #1266): the active `spans.jsonl` plus
    rotated `spans-<ISO-timestamp>.jsonl` backups, each line one
    `ExportTraceServiceRequest`. `meiva trace ship` replays a window of it
    straight to Tempo when the live fan-out missed data."""
    return home / "traces"


def otel_collector_dir(home: Path) -> Path:
    """Per-unit OTel Collector sidecar root (`<home>/otel-collector`).

    Owns the pinned otelcol-contrib binary, the generated config.yaml (from
    the repo template, baked at converge) and the file_storage filelog-offsets
    directory. One sidecar per machine — see
    `cli/commands/lifecycle/converge_host.py:_ensure_otel_collector_step`.
    Pure: the converge step that installs the binary creates the directory."""
    return home / "otel-collector"


def otel_collector_binary(home: Path) -> Path:
    """Path of the pinned otelcol-contrib binary."""
    return otel_collector_dir(home) / "otelcol-contrib"


def otel_collector_config(home: Path) -> Path:
    """Path of the generated sidecar config.yaml (baked at converge)."""
    return otel_collector_dir(home) / "config.yaml"


def memory_dir() -> Path:
    """Per-unit memory git repo root ($MEIVA_HOME/memory). Returns the path only;
    the caller (memory_repo) runs git init / clone.

    On a unit that also carries 'gateway' capability, this is the agent-runner's
    authoring checkout (machine-<name> branch); the gateway indexes from a
    separate consolidated checkout returned by gateway_memory_dir()."""
    return meiva_home() / "memory"


def gateway_memory_dir(*, roles: frozenset[str]) -> Path:
    """Gateway's consolidated memory checkout path. On a combined unit
    (gateway+agent-runner on one host) this is $MEIVA_HOME/gateway/memory to
    keep the gateway's index source (main branch) separate from the
    agent-runner's authoring checkout (machine-<name> branch at memory_dir()).
    On a gateway-only unit it is the same as memory_dir(). Returns the path
    only."""
    if "agent-runner" in roles:
        return meiva_home() / "gateway" / "memory"
    return memory_dir()


def skills_dir(home: Path) -> Path:
    """Per-unit skill load dir (`<home>/skills`) — the single directory the
    skill scanner reads; repo + plugin skills are converged into it. Pure: a
    writer creates it."""
    return home / "skills"


def mcps_dir(home: Path) -> Path:
    """Per-unit installed-MCP load dir (`<home>/mcps`) — where `meiva mcp install`
    lands each installed MCP package (`<name>/` holding its own `.mcp.json` +
    `pyproject.toml` + `.venv`). The MCP config loader scans each `<name>/.mcp.json`
    here as its installed layer, gated by the install registry; built-in MCPs stay in
    the repo's `<repo>/meiva_builtins/mcps/`. Symmetric with `skills_dir(home)` / `plugins_dir()`."""
    return home / "mcps"


def workspaces_root() -> Path:
    return meiva_home() / "workspaces"


def workspace_dir(agent_id: int) -> Path:
    """Per-agent workspace ($MEIVA_HOME/workspaces/<agent_id>); create if missing.

    A stable scratch area for files an agent produces or downloads. Survives
    restarts and termination, is not git-managed, and is never garbage-collected
    by the framework — cleanup is an ops decision. Cross-agent sharing happens
    by passing absolute paths in messages, not by writing into each other's
    workspace."""
    if agent_id is None:  # pyright: ignore[reportUnnecessaryComparison] — guards a bad call, type hint alone won't catch it at runtime
        raise ValueError(
            "workspace_dir(None) — pass a real agent_id. Check "
            "meiva.sdk_surface.agent_identity.agent_id() is not None first (pre-bootstrap has no "
            "workspace); callers that need a pre-bootstrap fallback use Path.home() "
            "explicitly instead of calling workspace_dir()."
        )
    target = workspaces_root() / str(agent_id)
    target.mkdir(parents=True, exist_ok=True)
    return target


def workspace_dir_readonly(agent_id: int) -> Path:
    """Per-agent workspace path ($MEIVA_HOME/workspaces/<agent_id>) with NO
    create side effect — the read path for ANOTHER agent's workspace (the
    inherited-memory note scans an ancestor's `memory/`). `workspace_dir`
    deliberately ensures its target; a reader must not manufacture a
    workspace that does not exist."""
    return workspaces_root() / str(agent_id)


def exec_run_dir(home: Path) -> Path:
    """Per-unit exec-subprocess scratch dir (`<home>/run/exec`) — request /
    result envelopes for one execute_code run each (agent/graph/exec/protocol.py),
    pruned per agent subdir. Pure: the writer of an envelope creates it."""
    return run_dir(home) / "exec"


def root_run_dir(home: Path) -> Path:
    """meiva-root supervisor runtime dir (`<home>/run/meiva-root`).

    One directory per (machine x home) unit: the control socket
    (`meiva-root.sock`), the instance lock (`meiva-root.lock`), the unit log tree
    (`logs/<unit>/output.log`) and the generated unit manifest
    (`manifests.json`) all live here, so the daemon's `--run-dir` argument is
    this single path. Pure: `meiva start` creates it before it writes the manifest."""
    return run_dir(home) / "meiva-root"


def root_manifests_path(home: Path) -> Path:
    """The K2 unit manifest the root supervisor reads (`<home>/run/meiva-root/manifests.json`).

    Regenerated by every root-driven `meiva start`
    (`services.supervision.meiva_root_glue.manifests.generate`) before the daemon is
    (re)spawned, so the running tree's shape always matches this file at
    launch time."""
    return root_run_dir(home) / "manifests.json"


def quarantined_exec_requests_dir(home: Path) -> Path:
    """Per-unit quarantine for exec request envelopes (`<home>/quarantined-exec-requests`).

    Stale evidence moved out of run/exec (base/agents/incarnation/exec_request_evidence.py): the
    files stay for inspection beside their JSON receipt, one event subdirectory
    per quarantine pass. Pure: the quarantine pass creates it."""
    return home / "quarantined-exec-requests"


def launch_failures_path(home: Path) -> Path:
    """Path of the `<home>/last_launch_failures` file — the session names the
    last `meiva start` on this host could not launch.

    Exists because the rollout's local leg runs `meiva start` in a FRESH child
    process (`update._boot_gateway_fresh`), and an exit code cannot carry names.
    The parent orchestration needs the names to put them in the ROLLOUT
    aftermath block, and it cannot compute them itself: its own interpreter is
    the pre-pull one, so its service roster is the old tree's. The child has the
    new tree's roster and writes what it actually failed to launch.

    Written by every `meiva start` (an empty failure set unlinks it), so a read is
    never stale; taken — read then unlinked — by whoever consumes it.

    Returns the path only; ``base.deploy.lifecycle.launch_failures`` owns read/write.
    """
    return home / "last_launch_failures"


def running_sha_path(home: Path) -> Path:
    """Path of the `<home>/running_sha` file that records the commit the
    gateway was last started on.

    Written by `meiva start` (on every host, regardless of role) right before
    launching services; read by the update change-detection path so a manual
    `git pull` between start and update cannot hide code changes (the diff is
    against the running commit, not the current HEAD). Absent file = first-ever
    start or uninitialised; the reader falls back to HEAD.

    Returns the path only; ``base.deploy.git.running_sha`` owns read/write.
    """
    return home / "running_sha"


def computer_mcp_socket(home: Path) -> Path:
    """Unix socket of the per-machine shared computer MCP service
    (`<home>/run/computer-mcp.sock`). The per-agent bridge / the MCP daemon's
    direct dial connect here; the computer-mcp daemon listens on it. One
    service per machine (the desktop is one shared screen), so no port key."""
    return run_dir(home) / "computer-mcp.sock"


def insights_socket(home: Path) -> Path:
    """Unix socket the insights service answers on (`<home>/run/insights.sock`).

    The service has no authentication of its own: the gateway authenticates the caller and
    proxies to it, and a socket in the home's run directory (mode 0600, bound by the service)
    is reachable by the home's owner only. The service binds here and the gateway dials here.
    """
    return run_dir(home) / "insights.sock"


def insights_pidfile(home: Path) -> Path:
    """Pidfile of the insights service (`<home>/run/insights.pid`)."""
    return run_dir(home) / "insights.pid"


def mcp_daemon_shared_socket(home: Path) -> str:
    """Filesystem path of the per-machine shared MCP daemon socket — one
    socket under `<home>/run` serving every agent on the machine (the daemon
    isolates sessions per client connection; see meiva/mcps/_daemon.py). The
    single source of truth for the naming convention: the daemon binds here and
    the client (meiva.mcps) connects here. Lives in base/ so the agent kernel
    can import it without going through the agent-facing `meiva.mcps`, which
    MEIVA_SDK_DISABLE may replace with a stub. Run as an agent-runner service
    (ops roster session "mcp-daemon"), watchdog-managed like browser-mcp."""
    return str(run_dir(home) / "mcp_daemon.sock")
