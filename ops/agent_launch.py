"""Agent process launch mechanics — the detached-spawn + launch-confirm layer
under `ops/agents.py`.

`ops/agents.py` owns the *orchestration* (spawn / resurrect / respawn:
DB state transitions + inbound delivery); this module owns the *mechanics* of
actually getting a child python process running and confirming it came up. An
agent process is non-interactive (I/O over DB + Redis, logs to a file), so it is
launched **detached and native**, via the platform's native process supervisor
(`shared.session_backend.native_proc()` → `posixproc` / `winproc`): the child is
double-forked to reparent onto init and its stdout/stderr land in
`$AVA_HOME/logs/`. Retiring the per-agent session frees the per-box PTY
ceiling (macOS `kern.tty.ptmx_max`) that used to bound agent count. Daemons and
the agents' own persistent shells live in per-session pty hosts (they want the PTY).

The split keeps each file within its line budget and isolates the "spawns a
child / polls agents_meta" surface that tests universally monkeypatch (the
autouse launch guard in `tests/conftest.py` replaces `_launch_agent_process`
here so no test starts a real process).

`ops/agents.py` reaches these via module-qualified access
(`agent_launch._launch_agent_process(...)`), never `from ... import` — so the
internal `_launch_or_force_terminated -> _launch_agent_process` cross-call and
the orchestrator's own calls both resolve through this module's namespace,
giving tests a single patch point (`ops.agent_launch.<symbol>`).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from uuid import uuid4

import psutil

import shared.db
from ops.agent_identity import AGENT_ID_FLAG, AGENT_MODULE_ARGV
from ops.pages import list_open_page_names
from shared.agents import AgentStatus
from shared.boot_timing import (
    BOOT_BUDGET_SEC,
    BOOT_REAP_GRACE_SEC,
    BOOT_STALL_SEC,
    LAUNCH_CONFIRM_TIMEOUT_SEC,
)
from shared.cluster import session_name
from shared.db_transaction import write_transaction
from shared.env_registry import AGENT_BIRTH_CONFIG_ENV, AGENT_CONFIG_OVERLAY_ENV
from shared.live_announce import publish_agent_updated_sync, publish_page_closed_sync
from shared.log import logger
from shared.paths import logs_dir, repo_root, run_dir
from shared.platform import IS_WINDOWS
from shared.session_backend import native_proc
from shared.session_record import SessionRecord

# After the child is spawned, `_launch_agent_process` polls `agents_meta.pid`
# to wait for the child python process to actually claim its row. A successful spawn means "the process was
# launched", **not** "that process actually started running"; without
# confirmation "spawn OK but child immediately crashes -> row permanently stuck
# in an unclaimed row can happen (agent 137 / agent 44 incident).
#
# The four clocks that order this window — the confirm deadline, the child's
# stall watchdog and boot budget, and the reaper's grace — live in
# `shared/boot_timing.py`, with their load-bearing orderings declared in
# `shared/timing.py`. This module only consumes them. Env overrides
# (`AVA_LAUNCH_CONFIRM_TIMEOUT_SECONDS`, ...) keep working through settings;
# tests monkeypatch `shared.boot_timing` values smaller to run faster.
_LAUNCH_CONFIRM_POLL_INTERVAL_SEC = 0.05

# How often the confirm poll asks the supervisor whether the child still exists.
# Coarser than the row poll because it is a syscall against the session record
# rather than a cheap read, and 1s of detection latency is nothing against the
# windows in play.
_LIVENESS_PROBE_INTERVAL_SEC = 1.0

# Launch is transient-failure-prone: within the launch window, the child hasn't
# claimed yet, resurrect/respawn races with the restarter, fork jitter, etc.,
# can all cause a single launch to fail. Directly force-terminating is too
# fragile — one transient glitch would mark the agent as terminated, requiring
# manual resurrection to revive (one of the root causes of agents becoming
# terminated after rollout). Here we retry launch failures up to
# _LAUNCH_MAX_RETRIES times, with exponential backoff, cleaning up stale
# sessions before each retry; force-terminate only after exhausting retries.
# Tests monkeypatch these two values to 0 to run faster.
_LAUNCH_MAX_RETRIES = 3
_LAUNCH_RETRY_BASE_BACKOFF_SEC = (
    1.0  # wait base * 2**i seconds on the i-th retry (0-indexed) -> 1, 2, 4
)


def _launched_process_alive(agent_id: int, attempt_session: str | None = None) -> bool:
    """True if the process this launch started is still running.

    The exact attempt record returned at spawn carries the child's pid + start
    time, so this answers
    "is the thing I launched still there" WITHOUT the DB — which is the whole
    point: during the pre-claim segment the row still says unclaimed 'idling' and carries
    no pid, so the DB cannot distinguish a slow boot from a dead one. Indirected
    through the module namespace so tests can stub the liveness answer.

    What this question means is set on the child's side, not here. On its own,
    "the process exists" cannot tell a boot that is 90% through its imports from
    one deadlocked on a DB connect. The child's boot watchdog closes that gap by
    killing itself when its boot stops progressing (`agent/_boot_deadline.py`),
    so within the pre-flip window a live process is a progressing one — and the
    corollary matters as much: weaken or disable that watchdog
    (`AVA_AGENT_BOOT_STALL_SECONDS=0`) and this call silently reverts to the
    proxy it used to be.
    """
    # Compatibility callers lacking the exact attempt cannot establish early
    # death. Retain the existing hard deadline instead of guessing canonical:
    # canonical publication happens only after admission.
    if attempt_session is None:
        return True
    if not attempt_session.startswith(session_name(f"boot-{agent_id}-")):
        raise ValueError("launch confirmation attempt belongs to another agent")
    return native_proc().has_session(attempt_session)


def _wait_for_agent_claim(agent_id: int, attempt_session: str | None = None) -> None:
    """Wait for a non-NULL pid that proves the child won the row claim.

    A terminal row without a pid failed before claim and remains a launch
    failure. A pid confirms a claim even when a fast process has already moved
    from running to idling or terminated before this poll sees it.
    """
    started = time.monotonic()
    deadline = started + LAUNCH_CONFIRM_TIMEOUT_SEC
    probed_alive_at = started
    while True:
        with shared.db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (agent_id,))
            row = cur.fetchone()
        if row is not None:
            status, pid = row
            if status == AgentStatus.TERMINATED and pid is None:
                raise RuntimeError(
                    f"agent {agent_id}: boot rejected before claiming its row — status "
                    f"'terminated' with no pid. The process exited before taking "
                    f"ownership (schema gate rejected a behind-schema boot, placement "
                    f"gate rejected a wrong-host launch, or it died pre-claim and was "
                    f"reaped). The agent never started; fix the host (e.g. let the "
                    f"watchdog run `ava cluster update` on a code/schema mismatch) and "
                    f"resurrect."
                )
            if pid is not None:
                # Confirmation timing: a creeping boot (import bloat, slow DB)
                # shows up as this duration drifting toward the timeout long
                # before launches start failing outright.
                logger.info(
                    "agent {id} launch confirmed: status={status} pid={pid} after {dt:.1f}s",
                    id=agent_id,
                    status=status,
                    pid=pid,
                    dt=time.monotonic() - started,
                )
                return
        if time.monotonic() - probed_alive_at >= _LIVENESS_PROBE_INTERVAL_SEC:
            probed_alive_at = time.monotonic()
            alive = (
                _launched_process_alive(agent_id, attempt_session)
                if attempt_session is not None
                else _launched_process_alive(agent_id)
            )
            if not alive:
                # Collapse the deadline instead of raising here: the next
                # iteration re-reads the row before the failure branch runs, so a
                # child that claimed and exited between this loop's read and this
                # probe is still recognised as the confirmed start it is.
                deadline = probed_alive_at
                continue
        if time.monotonic() >= deadline:
            # One extension for a live child: `deadline` becomes the hard bound,
            # so this branch cannot be taken twice.
            hard_deadline = started + BOOT_REAP_GRACE_SEC
            alive = (
                _launched_process_alive(agent_id, attempt_session)
                if attempt_session is not None
                else _launched_process_alive(agent_id)
            )
            if deadline < hard_deadline and alive:
                logger.warning(
                    "agent {id} is still unclaimed after {dt:.1f}s but its process is alive — "
                    "extending the launch confirm to {max:.0f}s (slow pre-flip boot; "
                    "a loaded box, not a failed launch)",
                    id=agent_id,
                    dt=time.monotonic() - started,
                    max=BOOT_REAP_GRACE_SEC,
                    event="launch_confirm_extended",
                )
                deadline = hard_deadline
                continue
            raise RuntimeError(
                f"agent {agent_id}: pid stayed NULL within "
                f"{time.monotonic() - started:.1f}s after launch / child python process did "
                f"not reach `claim_agent_row` (early import / config / startup crash, "
                f"the spawned process exited immediately, or its own boot watchdog exited "
                f"it after {BOOT_STALL_SEC:.0f}s with no boot progress). Its stderr "
                f"is at $AVA_HOME/logs/agent-{agent_id}.stderr.log — a watchdog exit names "
                f"the phase the boot stalled in there."
            )
        time.sleep(_LAUNCH_CONFIRM_POLL_INTERVAL_SEC)


# The agent SELF-FETCHES its config from the gateway at startup (not a forwarded
# snapshot), so a restarted agent picks up a rotated key without a relay.
# agent_spawn_env_dict builds the child env by POSITIVE allowlist (Task #856
# Phase C, audit F-s3-4 — the policy data lives in shared/env_registry.py
# `child_env("agent", ...)`): host-scope facts (machine identity, health ports,
# AVA_HOME, the gateway URL the fetch dials), the agent-scope knobs
# (AVA_LLM_OVERRIDE for the e2e fake model, ...), the boot-time guide keys
# (cluster secret), the overlay/birth JSON
# carriers, and the ambient display passthroughs. It never carries a
# config-source pin (the child derives it from its own role at Settings build;
# AVA_CONFIG_SOURCE is gone). Indirected below for test monkeypatching.
def agent_spawn_env_dict() -> dict[str, str]:
    """The child environment for a detached native agent process.

    Built from the registry's agent forward view
    (`shared.env_registry.child_env("agent", ...)`) — not "everything minus a
    drop set": a non-modeled knob (AVA_AGENT_ID, ...) never rides into an
    agent. The keys are copied from the dict outright (not blanked): omitting
    a key lets the child fall back to the field DEFAULT, where a blank string
    would fail the typed fields' parsing and crash the agent's Settings build.
    AVA_CLUSTER_SECRET is carried as the boot-time bearer. AVA_DB_URL is injected
    below as an ava_runner projection, never copied from the parent allowlist.
    """
    from shared.env_registry import child_env

    # The registry's agent forward view: session set + agent-scope knobs +
    # boot-time guide keys + the ambient display/temp-dir passthroughs
    # (non-empty only) + the Windows system keys on Windows.
    env = child_env("agent", "windows" if IS_WINDOWS else "posix")
    db_url = os.environ.get("AVA_DB_URL", "")
    if db_url:
        from shared.cluster.derive import runner_db_url_projection

        env["AVA_DB_URL"] = runner_db_url_projection(db_url)
    # Mark this process as an agent so shared.dotenv_boot does not apply the
    # gateway profile filter (the gateway pop would drop cluster-scoped
    # agent-runner keys that the agent needs to backfill from .env on a
    # single-box setup).
    env["AVA_PROCESS_PROFILE"] = "agent"
    # Windows UTF-8 mode needs no pin here: child_env's windows branch already
    # injects PYTHONUTF8=1 for every role (task #2540).
    return env


def _agent_interpreter() -> tuple[str, str, str]:
    """The venv python that runs the agent, its VIRTUAL_ENV root, and its bin dir.

    Launch execs the current installed runtime's absolute interpreter; editable
    development retains `<checkout>/.venv`. No child re-resolves a moving release
    selector and no launch performs a dependency sync. Installation and admission
    belong to the deployment workflow, not this process factory.

    Callers reproduce what `uv run` used to inject into the child env — the venv
    root as VIRTUAL_ENV and its bin dir prepended to PATH — so the agent's own
    env still "activates" the venv. This matters because `ava.shell.run` runs
    agent shell commands via a non-login `sh -c` that inherits this env: bare
    `python` / `ava` in agent commands must keep resolving into the venv.
    """
    from shared.runtime_interpreter import runtime_venv

    venv = runtime_venv()
    bindir = venv / ("Scripts" if IS_WINDOWS else "bin")
    python = bindir / ("python.exe" if IS_WINDOWS else "python")
    return str(python), str(venv), str(bindir)


def _launch_agent_process(
    agent_id: int,
    config_overlay: dict[str, object] | None = None,
    *,
    birth_config: dict[str, object] | None = None,
    confirm: bool = True,
    restart_attempt: tuple[int, int, float] | None = None,
    resurrect_attempt: tuple[int, int, float] | None = None,
) -> str:
    """Spawn a new detached agent process via the native supervisor.
    Raises RuntimeError if the spawn itself fails; does **not** clean up DB on
    failure.

    Each parent launch writes a unique `ava-boot-{id}-...` attempt record.
    Only the child admitted by PostgreSQL publishes `ava-agent-{id}`; a late
    parent or rejected boot cannot overwrite its successor's canonical record.
    The child is
    double-forked (POSIX) / detached (Windows) so it reparents to init and no
    zombie accretes in the long-lived gateway / ops daemon that spawned it.

    The venv python is exec'd directly (no resident `uv run` parent per agent).
    The child env reproduces `uv run`'s venv activation — VIRTUAL_ENV + the venv
    bin dir prepended to PATH — so subprocesses the agent spawns resolve
    `python` / `ava` into the venv (agent shell commands inherit this env via
    `ava.shell.run`; see `_agent_interpreter`). The config overlay + the birth
    stamp each ride in that env dict (`$AVA_AGENT_CONFIG_OVERLAY` /
    `$AVA_AGENT_BIRTH_CONFIG`) rather than on the world-readable argv — either
    JSON blob may carry something sensitive (issue #974) — and together they are
    the whole per-agent config the child replays (`config_overlay > birth_config
    > current config`). stderr is split to
    `$AVA_HOME/logs/agent-{id}.stderr.log` so a pre-death traceback survives the
    process ending (agent 152 incident); stdout goes to the session `.out.log`.

    With `confirm=True` (resurrect / respawn) it also polls
    `_wait_for_agent_claim` inline: a successful spawn only proves
    the process launched, not that the child claimed its row — in prod a launched
    child has crashed immediately and left the row unclaimed in 'idling' (agent
    137/44 incident). Those callers already run off the event loop, so blocking
    on the confirm is fine and gives an immediate "did the wake succeed" answer.
    Spawn passes `confirm=False` and confirms off-path via
    `schedule_launch_confirm`, so the spawn response is not held for the confirm
    window.
    """
    supervisor = native_proc()
    _require_released_agent_session(agent_id)
    if restart_attempt is not None and resurrect_attempt is not None:
        raise ValueError("one launch cannot belong to two lifecycle commands")
    bound_attempt = restart_attempt if restart_attempt is not None else resurrect_attempt
    if bound_attempt is None:
        agent_session = session_name(f"boot-{agent_id}-{uuid4().hex}")
    else:
        command_id, attempt_number, remaining_budget = bound_attempt
        if command_id <= 0 or attempt_number <= 0 or remaining_budget <= 0 or confirm:
            raise ValueError("invalid command-bound asynchronous restart attempt")
        # Parent publication can only touch this exact attempt, never the
        # canonical record published by the child that wins admission.
        agent_session = session_name(f"boot-{agent_id}-{command_id}-{attempt_number}")

    agent_python, venv_dir, venv_bin = _agent_interpreter()
    # The two boot windows ride argv rather than the env dict below, unlike every
    # secret-bearing value: the child must read them before importing
    # `shared.config`, since that import is part of the segment the watchdog they
    # arm exists to cover. argv is the only channel available that early, and a
    # timeout is not secret material (issue #974 governs the ones that are).
    #
    # The module + agent-id fragments come from `ops.agent_identity` because this
    # argv is also read back the other way: `probe_agent_process` matches them
    # against a live process's cmdline to tell this agent from whatever inherited
    # its pid. Spelling them literally here would let the launcher and the probe
    # drift apart silently (issue #1123).
    from shared.runtime_interpreter import WHEEL_RUNTIME

    argv = [
        agent_python,
        *(["-I", "-B", "-X", "utf8"] if WHEEL_RUNTIME else []),
        *AGENT_MODULE_ARGV,
        AGENT_ID_FLAG,
        str(agent_id),
        "--boot-stall-seconds",
        str(BOOT_STALL_SEC),
        "--boot-budget-seconds",
        str(BOOT_BUDGET_SEC),
    ]
    if restart_attempt is not None:
        argv[-1] = str(min(BOOT_BUDGET_SEC, restart_attempt[2]))
        argv.extend(["--restart-command-id", str(restart_attempt[0])])
    if resurrect_attempt is not None:
        argv[-1] = str(min(BOOT_BUDGET_SEC, resurrect_attempt[2]))
        argv.extend(["--resurrect-command-id", str(resurrect_attempt[0])])

    env = agent_spawn_env_dict()
    env["PYTHONMALLOC"] = "malloc"
    if config_overlay:
        # In the child's env, never its argv: an overlay may set any Settings
        # field (a provider api_key included) and `ps` shows argv to any local
        # user (issue #974). A child environment is owner-only on both platforms.
        env[AGENT_CONFIG_OVERLAY_ENV] = json.dumps(config_overlay, sort_keys=True)
    if birth_config:
        # Same reasoning as the overlay above: birth_config is framework-fields-
        # only today, but it rides the same env-not-argv path uniformly rather
        # than being a second, differently-treated JSON blob (issue #974).
        env[AGENT_BIRTH_CONFIG_ENV] = json.dumps(birth_config, sort_keys=True)
    # Reproduce uv run's venv activation: VIRTUAL_ENV + the venv bin dir prepended
    # to PATH. The PATH key can be "PATH" or "Path" on Windows — prepend onto
    # whichever the inherited env carries.
    env["VIRTUAL_ENV"] = venv_dir
    path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
    # PATH is NOT in the allowlist (not a Settings alias), so `env` has no
    # PATH key to prepend onto — reading it from the env dict would produce
    # a PATH of just the venv bin and leave the agent unable to find any
    # system binary (v0.1.34: every agent's PATH collapsed to
    # `<checkout>/.venv/bin`, `df`/`ssh`/`git` all gone). The child's PATH
    # must come from the spawner's own os.environ, venv bin first.
    env[path_key] = venv_bin + os.pathsep + os.environ.get(path_key, "")

    # Split stderr to the per-agent log so a Python uncaught traceback / loguru
    # human sink / C-level fputs(stderr) survives the process ending; stdout goes
    # to the session `.out.log`. The supervisor mkdirs $AVA_HOME/logs as a guard
    # for a fresh CI runner / brand-new machine where it does not yet exist.
    stderr_path = logs_dir() / f"agent-{agent_id}.stderr.log"
    ok = supervisor.new_session(
        agent_session,
        argv,
        repo_root(),
        env=env,
        stderr_append=stderr_path,
    )
    if not ok:
        raise RuntimeError(f"native supervisor failed to launch agent session {agent_session}")
    if confirm:
        _wait_for_agent_claim(agent_id, agent_session)
    return agent_session


# ── Off-path launch confirm (spawn) ──────────────────────────────────────────
#
# Spawn returns as soon as the row is committed and the process has launched; the
# launch-confirm then runs as a detached background task (off the event loop, in
# a worker thread) instead of blocking the spawn response. A confirm that fails
# forces the row 'terminated' so a silently-failed launch does not linger
# unclaimed 'idling'; the restarter's dead-birth reaper is the ultimate backstop if this
# task is ever lost.
_pending_launch_confirms: set[asyncio.Task[bool]] = set()


def _confirm_launch_or_force_terminated(agent_id: int, attempt_session: str | None = None) -> bool:
    """Confirm a spawned child claimed its row; if it never did, force the row
    'terminated'. Runs in a worker thread (synchronous poll + DB). Returns
    whether the launch confirmed."""
    try:
        if attempt_session is None:
            _wait_for_agent_claim(agent_id)
        else:
            _wait_for_agent_claim(agent_id, attempt_session)
        return True
    except Exception:
        logger.warning(
            "agent {id} launch did not confirm — forcing 'terminated' "
            "(child never claimed its row / died before claiming)",
            id=agent_id,
            event="launch_confirm_failed",
        )
    with write_transaction() as conn, conn.cursor() as cur:
        # Capture cascade-closable show() page names BEFORE the status flip.
        # Daemon-supervised serve() pages stay open across termination.
        # An unclaimed 'idling' row can hold open pages (resurrect's cascade_open
        # reopens them at the flip); the frontend popover needs the
        # PageClosed events to drop them.
        page_names = list_open_page_names(conn, agent_id)
        # Guard on unclaimed idling: a child that claimed just after the
        # confirm timeout is alive (rowcount 0) — leave it be. termination_source=
        # 'launch-confirm': an involuntary launch failure → crash-auto-resurrect
        # eligible (a re-launch attempt, spaced by the resurrect backoff).
        cur.execute(
            "UPDATE agents_meta SET status = 'terminated', termination_source = 'launch-confirm' "
            "WHERE id = %s AND status = 'idling' AND pid IS NULL "
            "AND runtime_generation IS NULL AND runtime_owner IS NULL AND runtime_kind IS NULL "
            "AND lifecycle_command_id IS NULL",
            (agent_id,),
        )
        if cur.rowcount == 1:
            conn.commit()
            publish_agent_updated_sync(conn, agent_id)
            for page_name in page_names:
                publish_page_closed_sync(agent_id, page_name)
    return False


def _on_confirm_done(task: asyncio.Task[bool]) -> None:
    _pending_launch_confirms.discard(task)
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.opt(exception=exc).error(
            "launch-confirm background task crashed", event="launch_confirm_task_crashed"
        )


def schedule_launch_confirm(agent_id: int, attempt_session: str | None = None) -> None:
    """Run the launch-confirm off the spawn response path. Must be called from a
    running event loop (the ops dispatch loop): it schedules a background task
    that polls in a worker thread and forces 'terminated' if the child never
    claims."""
    task = asyncio.create_task(
        asyncio.to_thread(_confirm_launch_or_force_terminated, agent_id, attempt_session)
    )
    _pending_launch_confirms.add(task)
    task.add_done_callback(_on_confirm_done)


def _launch_or_force_terminated(
    agent_id: int,
    config_overlay: dict[str, object] | None = None,
    *,
    birth_config: dict[str, object] | None = None,
) -> None:
    """Launch agent process; retry transient launch failures, and only after
    exhausting all retries UPDATE a still-unclaimed 'idling' row to 'terminated' +
    re-raise.

    Caller (resurrect_agent / respawn_agent) has already committed DB state
    (status='idling', lifecycle inbound INSERT pending). A launch failure
    leaves the agent stuck in unclaimed 'idling' with no process — neither restarter
    polls touch this state, so the agent would be silently abandoned.

    A single launch failure is often transient (the fork momentarily failing,
    the child not yet claiming inside the confirm window, a restart/reaper race),
    so we retry `_LAUNCH_MAX_RETRIES` times with exponential backoff, killing any
    leftover session before each retry. Only a RuntimeError is retried — that is
    the launch layer's own failure signal (`_launch_agent_process` raises
    RuntimeError on spawn failure / confirm timeout). Any OTHER exception
    is an unexpected bug: it propagates immediately without retry and without
    forcing 'terminated', so the fault surfaces loudly with state intact for
    investigation (fail fast).

    Forcing status='terminated' after retries are exhausted:
      1. Caller can retry via `resurrect_agent` (which expects 'terminated' →
         unclaimed 'idling'); the retry will INSERT a fresh lifecycle inbound on top
         of any leftover one — claim handles a batch of both naturally.
      2. Operator sees 'terminated' status (observable in monitoring) instead
         of an undefined "process never came up" state.
      3. The original launch RuntimeError still surfaces via re-raise so the
         operator gets the full stack trace.
      4. …but ONLY while the row is still unclaimed 'idling'. "Every attempt failed"
         is the launcher's local view; a child it gave up on can have claimed
         the row a moment later, and each terminate-write in this codebase
         carries the status predicate that keeps it from clobbering that.
         Whoever owns the row now (a live child, or the reaper) wins, and the
         re-raise still tells the caller this launch did not confirm.

    The spawn-launch op does NOT use this helper — its current policy is "a
    launch failure does not clean up DB" so the operator can investigate why a brand-new agent
    never started; for resurrect/respawn the agent already existed and operator
    care is shifted to "did the wake-up succeed", not "why did spawn fail".
    """
    for attempt in range(_LAUNCH_MAX_RETRIES + 1):
        try:
            _launch_agent_process(
                agent_id, config_overlay=config_overlay, birth_config=birth_config
            )
            return
        except RuntimeError as exc:
            # Last attempt still failed -> force-terminate + re-raise.
            if attempt >= _LAUNCH_MAX_RETRIES:
                logger.error(
                    "agent {id} launch failed after {total} attempts — forcing 'terminated': {exc}",
                    id=agent_id,
                    total=_LAUNCH_MAX_RETRIES + 1,
                    exc=repr(exc),
                    event="launch_force_terminated",
                )
                with write_transaction() as conn, conn.cursor() as cur:
                    # Capture cascade-closable show() page names BEFORE the
                    # status flip. Daemon-supervised serve() pages stay open;
                    # resurrect reopens only show() rows the cascade closed.
                    page_names = list_open_page_names(conn, agent_id)
                    cur.execute(
                        # 'launch-confirm': retries exhausted, the wake never came up
                        # → involuntary → crash-auto-resurrect eligible (backoff-spaced).
                        #
                        # Guarded on unclaimed idling exactly like the off-path
                        # confirm's write: by the time the last attempt gives up, one
                        # of the children it launched may have claimed the row just
                        # past its deadline and be running. An unguarded write buried
                        # that live agent under 'terminated', and crash-resurrect then
                        # launched a SECOND process for an agent that was already up —
                        # a duplicate launch manufactured by the cleanup itself.
                        "UPDATE agents_meta SET status = 'terminated', "
                        "termination_source = 'launch-confirm' "
                        "WHERE id = %s AND status = 'idling' AND pid IS NULL "
                        "AND runtime_generation IS NULL AND runtime_owner IS NULL AND runtime_kind IS NULL "
                        "AND lifecycle_command_id IS NULL",
                        (agent_id,),
                    )
                    if cur.rowcount == 1:
                        conn.commit()
                        publish_agent_updated_sync(conn, agent_id)
                        for page_name in page_names:
                            publish_page_closed_sync(agent_id, page_name)
                    else:
                        logger.warning(
                            "agent {id} was claimed while its launch was being "
                            "retried — a child claimed the row late, or the reaper "
                            "took it. Not forcing 'terminated'.",
                            id=agent_id,
                            event="launch_force_terminated_skipped",
                        )
                raise
            # Still have retries: clean up stale session, exponential backoff, then retry.
            backoff = _LAUNCH_RETRY_BASE_BACKOFF_SEC * 2**attempt
            logger.warning(
                "agent {id} launch attempt {n}/{total} failed ({exc}); "
                "requiring released session before retrying in {backoff:.0f}s",
                id=agent_id,
                n=attempt + 1,
                total=_LAUNCH_MAX_RETRIES + 1,
                exc=repr(exc),
                backoff=backoff,
                event="launch_retry",
            )
            _require_released_agent_session(agent_id)
            time.sleep(backoff)


def _require_released_agent_session(agent_id: int) -> None:
    """Refuse live/unknown canonical observations; never signal by session name.

    This preflight is not ownership or a reservation. A later competitor cannot
    be overwritten: parent records use unique attempt names and only the DB
    admission winner may publish canonical observation under its bounded lock.
    """
    path = run_dir() / "sessions" / f"{session_name(f'agent-{agent_id}')}.json"
    record = SessionRecord.read(path)
    if record is None:
        if path.exists():
            raise RuntimeError("canonical agent session record is unreadable")
        return
    try:
        process = psutil.Process(record.pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return
        identity = record.identifies(record.pid)
        if identity is False:
            return
        if identity is None and record.starttime is not None:
            raise RuntimeError("canonical agent session identity is unreadable")
        if (
            identity is None
            and record.create_time > 0
            and process.create_time() != record.create_time
        ):
            return
    except psutil.NoSuchProcess:
        return
    raise RuntimeError("canonical agent session is still live or its birth identity is unknown")
