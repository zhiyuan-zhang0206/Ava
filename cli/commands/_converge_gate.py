"""Converge step: the always-up fleet UI gate (`services.gate`).

The gate owns the entry port (the `frontend` slot, e.g. :3000) and proxies to
the Next.js app on the `app` slot (entry+1). It survives updates BY
CONSTRUCTION — it is not a service session and nothing in the update orchestration
stops or restarts it — so a rollout (gateway 503 + frontend restart) never
blacks out the entry: users see the static updating page and land back on the
app when the rollout finishes.

This step:
  1. backfills the cluster record's `app` slot (records born before the slot
     existed lack it) and materializes `AVA_APP_PORT` into the unit `.env`
     (`derive_env` only runs at install time),
  2. registers the gate under the platform supervisor — a launchd KeepAlive
     LaunchAgent on macOS, a user-systemd unit on Linux, and a detached process
     only on other POSIX systems.

**Step 2 replaces the job only when the desired plist actually changed.** That is
what "survives updates BY CONSTRUCTION" costs to keep true: converge runs on every
`ava start` and inside every rollout, so a registration that boots the job out and
back in unconditionally makes this step the one thing in the update path that CAN
black out the entry — and on 2026-08-01 it did (see `_bootout_and_wait` for the
launchd race, and `_ensure_launchd` for what it now raises instead of logging).

Which makes "changed" the load-bearing word, and it has to mean *the gate changed* —
not merely "its ports moved". The plist therefore also carries a content hash of
`services/gate/` (`_gate_content_hash`): a rollout that rewrites the login page or
`daemon.py` moves neither the checkout path nor the ports, so without it the desired
plist is byte-identical, converge correctly does nothing, and the old process serves
the old pages until a human restarts it.

`probe_gate` is the read side of the same fact, for `ava status` and the health
probe's alert-only check: the entry port had no monitor at all, so the outage was
invisible on every operator surface while the app slot behind it read green.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

from cli.commands._converge_spec import ConvergeCtx
from shared.config import settings
from shared.envfile import upsert_env
from shared.platform_backend import get_backend
from shared.resilience import ExponentialBackoff, Policy, http_classifier, retry

# The detached gate's argv marker — what `_ensure_detached` launches and what
# `gate_pid_is_ours` recognises it by.
_GATE_DAEMON_MODULE = "services.gate.daemon"

logger = logging.getLogger("cli.converge.gate")

_GATE_LABEL_PREFIX = "com.ava.gate"

# This module's sleeps, bound at import so a test can replace THIS throttle and
# nothing else. Patching `cli.commands._converge_gate.time.sleep` would resolve
# `time` to the stdlib module object and disable sleeping process-wide — the seam
# rule `cli.commands._probe._poll_sleep` states in full, and the incident behind it.
_sleep = time.sleep

# How long to wait for launchd to forget a booted-out job before loading its
# replacement, and how often to ask. Seconds rather than milliseconds because the
# job being waited on is a live HTTP server draining its connections.
_BOOTOUT_TIMEOUT_S = 10.0
_BOOTOUT_POLL_INTERVAL_S = 0.25

# Backoff before each bootstrap RETRY; its length sets the retry count (so three
# attempts in total). Increasing, because the failure it covers is a teardown that
# is taking longer than `_BOOTOUT_TIMEOUT_S` — waiting is the whole remedy.
_BOOTSTRAP_BACKOFF_S = (1.0, 3.0)


def gate_label(home: Path) -> str:
    """launchd label for one cluster's gate — ``com.ava.gate.<home-slug>``."""
    from shared.cluster import home_slug

    return f"{_GATE_LABEL_PREFIX}.{home_slug(home)}"


def _plist_path(home: Path) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{gate_label(home)}.plist"


def _gate_python() -> str:
    """The venv python of THIS checkout — the gate must run the checkout that
    owns the home (same anchor rule as `os_cron.ava_binary_path`)."""
    from shared.paths import repo_root

    return str(repo_root() / ".venv" / get_backend().venv_bin_dir_name() / "python")


def _gate_source_dir() -> Path:
    """The gate's own source tree in THIS checkout — same anchor rule as
    `_gate_python`, since that is the code the registered job will run."""
    from shared.paths import repo_root

    return repo_root() / "services" / "gate"


def _gate_content_hash(source_dir: Path | None = None) -> str:
    """sha256 over every file under `services/gate/` — what the gate IS, as a value
    the plist can carry.

    Nothing reads it back: the daemon never looks at `AVA_GATE_CONTENT_HASH`, and it
    is not a version to compare against. Its only job is to make the *desired plist*
    change whenever the gate's code or assets change, so that a gate change reaches
    `_ensure_launchd`'s existing "replace only when the plist actually changed" rule
    and gets the whole replacement path — the bootout, the wait for launchd to forget
    the label, the bootstrap retries, the fail-fast — exactly as a port move would.
    This completes that idempotence rather than routing around it: without the hash
    the rule is not too strict, it is being asked a question (did the ports move?)
    that has nothing to do with whether the running gate is stale.

    **The whole directory, not just `static/`.** The pages were the visible half of
    the gap, but `daemon.py` has the identical property: `Gate.__init__` reads all
    four static files once and inlines `theme.css` / `theme.js` into each page at
    load, so both the assets AND the code that assembles them are frozen into the
    process at boot and change nothing until it restarts. Proxy behaviour, the auth
    probe, the inlining mechanism itself — all restart-only. The gate's behaviour is
    the content of this directory, so the directory is the unit that gets hashed.

    `tree_hash` is this repo's one deterministic tree hash (sorted relative path +
    bytes; `__pycache__` / `.git` / `.DS_Store` excluded, which is what keeps a
    daemon that has been imported once from reading as a content change). It lives
    beside the install registry that first needed it; converging the skills tree is
    the same question asked about a different directory.
    """
    from shared.install_registry import tree_hash

    return tree_hash(source_dir or _gate_source_dir())


def _plist_content(home: Path, repo: Path) -> str:
    """KeepAlive LaunchAgent: the gate is the one process that must never be
    down during an update, so launchd restarts it on crash and runs it at load.

    Carries the gate's content hash so that "the plist changed" and "the gate
    changed" are the same question — see `_gate_content_hash`."""
    from shared.os_cron import launchd_env_block

    log_file = home / "logs" / "gate.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{gate_label(home)}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{_gate_python()}</string>
        <string>-m</string>
        <string>services.gate.daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{repo}</string>
{launchd_env_block(extra={"AVA_GATE_CONTENT_HASH": _gate_content_hash()})}
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_file}</string>
    <key>StandardErrorPath</key>
    <string>{log_file}</string>
</dict>
</plist>
"""


def _ensure_app_port(home: Path) -> int:
    """Backfill the record's `app` slot + materialize AVA_APP_PORT in the .env.

    Returns the app port. Raises when the home has no registry record — the
    gate cannot derive ports for a cluster that was never born.
    """
    from shared.cluster import (
        load_registry,
        record_app_port,
        registry_lock,
        save_record_locked,
    )

    # One critical section: load-modify-save must not interleave with a
    # concurrent birth/destroy (audit 2026-08-08 P2 — save_record without the
    # lock could clobber a fresh record with a stale snapshot).
    with registry_lock():
        registry = load_registry()
        rec = registry.get(str(home.expanduser()))
        if rec is None:
            raise RuntimeError(f"no registry record for {home} — cannot derive the gate's app port")
        app_port = record_app_port(rec)
        if rec.ports.get("app") is None:
            rec.ports["app"] = app_port
            save_record_locked(rec)
            logger.info("backfilled app port %d into %s's registry record", app_port, home)
    upsert_env(
        home / ".env",
        {"AVA_APP_PORT": str(app_port)},
        audit_site="converge_gateway_port",
    )
    return app_port


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # internal launchctl argv
        ["launchctl", *args], capture_output=True, text=True, check=False
    )


def _job_loaded(label: str) -> bool:
    """Whether launchd currently holds a job under `label`.

    `launchctl print` exits non-zero for a label the domain does not know, which
    is the only question asked here — the (large) dump it writes on success is
    ignored.
    """
    return _launchctl("print", f"gui/{os.getuid()}/{label}").returncode == 0


def _bootout_and_wait(label: str) -> bool:
    """Unload `label`, then wait until launchd has actually forgotten it.

    `bootout` returns once launchd has *accepted* the request, not once the job is
    gone: a gate with connections open is still draining, and a `bootstrap` of the
    same label inside that window fails with `Bootstrap failed: 5: Input/output
    error`. That is the 2026-08-01 race — the old gate was killed, its replacement
    never loaded, and the entry port went dark for the rest of the rollout.

    Returns whether the job is gone by the deadline. False is not fatal by itself:
    the bootstrap that follows is the step that has to succeed, and it is retried.
    """
    _launchctl("bootout", f"gui/{os.getuid()}/{label}")
    deadline = time.monotonic() + _BOOTOUT_TIMEOUT_S
    while _job_loaded(label):
        if time.monotonic() >= deadline:
            return False
        _sleep(_BOOTOUT_POLL_INTERVAL_S)
    return True


def _ensure_launchd(home: Path, repo: Path) -> None:
    """Bring the KeepAlive LaunchAgent to the desired plist, replacing the running
    job only when that plist actually changed.

    **An unchanged gate is never touched** — no bootout, no bootstrap, not even a
    rewrite of the plist. The plist carries the checkout path, the ports, and a hash
    of the gate's own source tree (`_gate_content_hash`), so a rollout that moves
    none of the three is a no-op and the gate process keeps serving across the
    update it exists to cover. Replacing a job whose spec is identical buys nothing
    and costs the entry port a hole.

    When the plist did change — or nothing is loaded — the swap waits for launchd to
    forget the old job before loading the new one (`_bootout_and_wait`) and retries
    the load with backoff. A swap that still never lands **raises**: converge is
    fail-fast, and the alternative is what prod got on 2026-08-01 — a `logger.error`,
    a rollout that reported `rc=0`, and an entry port with nothing listening on it.
    """
    from shared.os_cron import os_jobs_enabled, skip_os_job

    if not os_jobs_enabled():
        skip_os_job("gate LaunchAgent")
        return
    plist_path = _plist_path(home)
    label = gate_label(home)
    desired = _plist_content(home, repo)
    if plist_path.exists() and plist_path.read_text() == desired and _job_loaded(label):
        logger.info("gate LaunchAgent '%s' already current — leaving the gate running", label)
        return

    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(desired)
    attempts = len(_BOOTSTRAP_BACKOFF_S) + 1
    last_failure = ""
    for attempt in range(1, attempts + 1):
        if not _bootout_and_wait(label):
            logger.warning(
                "gate LaunchAgent '%s' still loaded %.0fs after bootout — bootstrapping anyway",
                label,
                _BOOTOUT_TIMEOUT_S,
            )
        result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(plist_path))
        if result.returncode == 0:
            logger.info("gate LaunchAgent '%s' loaded (KeepAlive)", label)
            return
        last_failure = f"rc={result.returncode}: {result.stderr.strip()}"
        logger.warning(
            "launchctl bootstrap failed for %s (attempt %d/%d, %s)",
            label,
            attempt,
            attempts,
            last_failure,
        )
        if attempt < attempts:
            _sleep(_BOOTSTRAP_BACKOFF_S[attempt - 1])
    raise RuntimeError(
        f"gate LaunchAgent '{label}' would not load after {attempts} attempts "
        f"({last_failure}) — the fleet UI entry port has no gate on it. "
        f"Inspect `launchctl print gui/{os.getuid()}/{label}` and {plist_path}."
    )


def gate_pid_is_ours(pid: int, repo: Path) -> bool:
    """Whether `pid` is really the gate daemon THIS checkout launched.

    A pidfile holds a bare integer and the OS recycles pids, so a pidfile left by
    a daemon that died without clearing it eventually names an unrelated process.
    Both sides of the gate's non-macOS lifecycle act on that number —
    `_ensure_detached` skips the launch when it looks alive, `stop_gate_service`
    SIGTERMs and then SIGKILLs it — so trusting it bare costs twice over: one
    recycled pid both signals a stranger and wedges the gate off its entry port,
    since only a successful launch rewrites the pidfile.

    The tokens are the daemon's own launch shape (`_ensure_detached` below):
    `python -m services.gate.daemon` started with `cwd=<repo>`. The module marker
    proves it is a gate; the working directory proves it is the checkout that owns
    this home. Compared as a resolved path, never as a substring — one checkout
    path is routinely a prefix of another's.

    A process this user cannot introspect counts as NOT ours: a gate that outlives
    its stop is visible on its entry port and idempotently re-converged;
    signalling a stranger is neither."""
    import psutil

    try:
        proc = psutil.Process(pid)
        argv = proc.cmdline()
        if argv[1:] != ["-m", _GATE_DAEMON_MODULE]:
            return False
        return Path(proc.cwd()).resolve() == repo.resolve()
    except (psutil.Error, OSError):
        return False


def gate_pid(home: Path, repo: Path) -> int | None:
    """The pid of the live gate daemon this checkout owns, or None.

    The single seam both the launch and the teardown read. A pidfile that no
    longer names our daemon — unparseable, process gone, or the number since
    recycled (`gate_pid_is_ours`) — reads as None and is removed, so the next
    converge launches a gate instead of skipping forever."""
    pidfile = home / "run" / "gate.pid"
    if not pidfile.exists():
        return None
    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, OSError):
        pidfile.unlink(missing_ok=True)
        return None
    if gate_pid_is_ours(pid, repo):
        return pid
    pidfile.unlink(missing_ok=True)
    return None


def _ensure_detached(home: Path, repo: Path) -> None:
    """Non-macOS POSIX gateways: a detached process with a pidfile, re-ensured
    on every converge (no launchd KeepAlive there — crash recovery waits for
    the next `ava start` / `ava cluster update`)."""
    if gate_pid(home, repo) is not None:
        return
    pidfile = home / "run" / "gate.pid"
    log_file = home / "logs" / "gate.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("ab") as log:
        proc = subprocess.Popen(  # internal argv
            [_gate_python(), "-m", _GATE_DAEMON_MODULE],
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pidfile.write_text(str(proc.pid))
    logger.info("gate started detached (pid %d)", proc.pid)


def ensure_gate(ctx: ConvergeCtx) -> None:
    """Converge step — see the module docstring. Gateway role only (the gate
    fronts the fleet UI; a pure agent-runner has no entry port to own)."""
    from shared.platform import IS_WINDOWS

    if IS_WINDOWS:
        return  # gateway is POSIX-only; defensive
    repo = ctx.repo
    home = Path(ctx.ava_home)
    _ensure_app_port(home)
    if sys.platform == "darwin":
        _ensure_launchd(home, repo)
    elif sys.platform == "linux":
        from cli.commands._gate_systemd import ensure

        ensure(home, repo, _gate_python(), _gate_content_hash())
    else:
        _ensure_detached(home, repo)


class GateStatus(NamedTuple):
    """What can be observed about this cluster's gate from outside it.

    `serving` is "the entry port answered HTTP at all" — **not** "answered 2xx".
    The gate's job during a rollout is to serve the 503 updating page and the 502
    the app proxy falls back to, so a 2xx test would call a correctly working gate
    dead at exactly the moment it is doing the one thing it exists for. What that
    signal cannot say is *whose* gate answered: the entry port is the public
    bookmark, bound on all interfaces, so it carries no identity payload the way a
    loopback `/healthz` daemon does (`shared.daemon_health.probe_daemon`) and this
    is liveness-only by property of the endpoint.

    `supervised` is the other half — whether this cluster's own supervisor still
    holds the gate (the launchd label on macOS, the user-systemd unit on Linux). The two
    fail independently and neither implies the other: a crash-looping gate is
    supervised and dark, and a gate whose job was booted out but whose process
    survives is serving with nothing left to restart it.

    Attributes:
        entry_port: the port the gate binds, read from the same `.env`-backed
            derivation the daemon itself uses (`services.gate.daemon.entry_port`).
        app_port: the Next.js slot the gate proxies to — context for the row, not
            probed here (the app has its own service row).
        serving: something answered HTTP on `entry_port`.
        supervised: this cluster's supervisor holds the gate.
        supervisor: which supervisor that is, in words, for the operator surface.
    """

    entry_port: int
    app_port: int
    serving: bool
    supervised: bool
    supervisor: str


# Transport-only confirm-retry (R2-D): ANY HTTP status counts as serving
# (see `GateStatus.serving`), so no status is retryable — only connection
# failures get one 1s confirm.
_ENTRY_PROBE_POLICY = Policy(
    max_attempts=2,
    backoff=ExponentialBackoff(base=1.0, factor=1.0, cap=1.0),
    jitter="none",
    classify=http_classifier.with_(transient=frozenset()),
    respect_retry_after=False,
)


def _entry_answers(port: int) -> bool:
    """Whether anything answers HTTP on the entry port — ANY status counts, for the
    reason `GateStatus.serving` gives.

    Confirm-retry (R2-D): a transient refusal / dropped connection at the
    probe instant must not read as a dead gate — one 1s confirm retry for
    transport errors only; ANY HTTP status (even 5xx) already counts as
    serving, so statuses are never retried here.
    """
    import httpx

    def _get() -> None:
        httpx.get(f"http://127.0.0.1:{port}/", timeout=5.0, follow_redirects=False)

    try:
        retry(_ENTRY_PROBE_POLICY)(_get)
    except httpx.HTTPError as exc:
        logger.warning("entry probe :%d failed: %s", port, exc)
        return False
    return True


def probe_gate(home: Path | None = None) -> GateStatus:
    """Observe the gate of the cluster at `home` — see `GateStatus`.

    The ports come from `services.gate.daemon`'s own accessors rather than a second
    derivation here, so what is probed is by construction the port the gate binds.
    """
    from services.gate.daemon import app_port, entry_port

    target = Path(home or settings.general.ava_home).expanduser()
    entry = entry_port()
    serving = _entry_answers(entry)
    if sys.platform == "darwin":
        label = gate_label(target)
        return GateStatus(entry, app_port(), serving, _job_loaded(label), f"launchd job {label}")
    if sys.platform == "linux":
        from cli.commands._gate_systemd import supervised, unit_name

        return GateStatus(
            entry, app_port(), serving, supervised(target), f"systemd user unit {unit_name(target)}"
        )
    pidfile = target / "run" / "gate.pid"
    return GateStatus(entry, app_port(), serving, _pidfile_alive(pidfile), f"pidfile {pidfile}")


def _pidfile_alive(pidfile: Path) -> bool:
    """Whether `pidfile` names a live process (the non-macOS gate's supervision)."""
    from shared.proc import process_alive

    try:
        return process_alive(int(pidfile.read_text().strip()))
    except (FileNotFoundError, ValueError):
        return False


def print_gate_status() -> None:
    """Print the gate's entry-port view for `ava status`.

    The gate owns the port the user has bookmarked, and until this section existed
    nothing on any operator surface looked at it: `ava status` probed the Next.js
    app on the `app` slot, which stays perfectly green while the entry in front of
    it is dark. That is how the 2026-08-01 outage read from `ava status` — every
    row a ✓, and the fleet UI unreachable.

    Two lines rather than one because `serving` and `supervised` fail
    independently (`GateStatus`), and the remedy differs: a dark entry with the job
    loaded is a crashing gate (read `$AVA_HOME/logs/gate.log`), a missing job is a
    registration that never landed (`ava converge`).
    """
    from shared.platform import IS_WINDOWS

    if IS_WINDOWS:
        print("  · skipped (the gate is POSIX-only)")
        return
    status = probe_gate()
    if status.serving:
        print(f"  ✓ entry :{status.entry_port} answering (proxies app :{status.app_port})")
    else:
        print(
            f"  ✗ entry :{status.entry_port} not answering — the fleet UI is dark "
            f"(read $AVA_HOME/logs/gate.log, then `ava converge`)"
        )
    if status.supervised:
        print(f"  ✓ supervised by {status.supervisor}")
    else:
        print(f"  ✗ not supervised — {status.supervisor} is absent (run `ava converge`)")


def unregister_gate(home: Path | None = None) -> None:
    """Remove the gate's OS registration for the cluster at `home` (destroy
    path). ``home`` is explicit — same rule as the other unregister helpers:
    settings cannot be re-pointed mid-process."""
    from shared.platform import IS_WINDOWS

    if IS_WINDOWS:
        return
    target = Path(home or settings.general.ava_home).expanduser()
    if sys.platform == "darwin":
        # Through `_launchctl`, like every other launchd call here: one seam means a
        # test can never boot out the operator's real job (`tests/cli/conftest.py`).
        _launchctl("bootout", f"gui/{os.getuid()}/{gate_label(target)}")
        _plist_path(target).unlink(missing_ok=True)
    elif sys.platform == "linux":
        from cli.commands._gate_systemd import _stop_legacy, stop
        from shared.paths import repo_root

        stop(target, remove=True)
        _stop_legacy(target, repo_root())
    else:
        import contextlib

        pidfile = target / "run" / "gate.pid"
        if pidfile.exists():
            from shared.proc import process_alive

            with contextlib.suppress(ValueError, ProcessLookupError):
                pid = int(pidfile.read_text().strip())
                if process_alive(pid):
                    os.kill(pid, 15)
            pidfile.unlink(missing_ok=True)
