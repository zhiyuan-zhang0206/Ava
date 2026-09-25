"""Channel C's unit-side handlers: start one unit's restricted hop session, and
report its bootstrap-recovery journal slot read-only.

The daemon-side half of task #4129 channel C (design
`managed-writer-dispatch-design-20260920.md` section 3.3): verify the payload's
request path as canonical private unit state (the shared
`ops.unit_local.private_unit_reference` stat face), then spawn the detached
`ava-updater` session that runs the retained candidate image's `--bootstrap-hop`
entry against it.

The handler never reads the request's content, never pauses anything and seeds
no handoff of its own: the request is a request, and the child's own
compare-and-set (`begin_bootstrap_after_dead_owner`) plus
`prepare_bootstrap_hop`'s re-read of the live rollout lease are what refuse a
live or unproven owner. That is also what keeps the coordinator's pre-stop
abort exact (task #4129 C-4): until the child's first effect, this unit's whole
footprint of the hop is one private file it did not write plus a process that
will refuse rather than act.

Linux-only by construction: the restricted hop's native proof (cron ownership,
A/B observation) has no other platform's arm, so the platform check refuses
before anything is spawned -- the POSIX command spelling in
`spawn_bootstrap_hop` is reached only past this gate.

The op schemas live here too, folded from `ops/rpc_bootstrap_hop.py` at the
structure budget (task #4129 I6); the receipt is the spawn fact, never a
generation -- the coordinator's C-3 generation binding reads the I5 ledger
instead. The detached entry spawns (`spawn_bootstrap_hop`,
`spawn_normal_continue`) were folded here from `ops/updater_entries.py` (itself
split out of `ops/cluster_deploy.py` at the file-size budget): the hop spawn is
the deploy family's only non-deploy trigger -- it pauses nothing, seeds no
handoff, and refuses rather than acts while the coordinator's exact pre-stop
abort (task #4129 C-4) still holds -- and the normal-continuation steps share
that discipline. `ops.cluster` re-exports the two spawns from here for its
existing importers, on the same eager re-export terms as its siblings.
"""

from __future__ import annotations

import logging
import os
import shlex
import sys
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

import shared.cluster
import shared.ui_update_state
from ops import cluster_session, unit_local
from ops.cluster_session import _UPDATER_SERVICE
from shared.config import settings
from shared.log import logger
from shared.managed_writer_barrier import Digest
from shared.runtime_release import ReleaseRejectedError


class BootstrapHopPayload(BaseModel):
    """``cluster_bootstrap_hop`` payload: the unit-local request path.

    `hop_request` names one existing private file inside this unit's `run/`
    directory (`{home}/run/...`, 0600, owned); `artifact_digest` selects the
    retained image whose interpreter runs the hop. Both locate and launch --
    the entry re-verifies the whole context from the request bytes itself.
    """

    model_config = ConfigDict(extra="forbid")

    hop_request: str = Field(min_length=1, max_length=4096)
    artifact_digest: Digest


class BootstrapHopResult(BaseModel):
    """``cluster_bootstrap_hop`` result: the spawned hop session, as facts.

    `session` is the detached `ava-updater` session's name and `log` its append
    target. No generation here: only the child's own CAS produces one.
    """

    model_config = ConfigDict(extra="forbid")

    machine: str = Field(min_length=1, max_length=128)
    home: str = Field(min_length=1, max_length=4096)
    session: str = Field(min_length=1, max_length=128)
    log: str = Field(min_length=1, max_length=4096)


class BootstrapRecoveryReadPayload(BaseModel):
    """``cluster_bootstrap_recovery_read`` payload: no arguments.

    The question is about the unit's own journal slot, so the payload carries
    nothing -- the envelope's target machine is the addressing, and the answer
    is one read-only unit-local fact.
    """

    model_config = ConfigDict(extra="forbid")


class BootstrapRecoveryReadResult(BaseModel):
    """``cluster_bootstrap_recovery_read`` result: this unit's bootstrap-recovery journal slot.

    `journal_present` is the effect marker the exact pre-stop abort requires
    absent on every unit: the hop child's first durable write is this journal
    (before it, every action was staged-file writes or read-only checks).
    `journal_stage` rides along when the journal is readable, for the
    operator's diagnosis; `normal_release_stage` is the nested normal-release
    continuation's stage (None until that continuation has written one), which
    the coordinator's commit-tail wait reads (task #4129 I6). A
    present-but-unreadable journal still reports `journal_present=True` (with
    no stage) -- an effect is reported as the fact it is, never an op failure.
    """

    model_config = ConfigDict(extra="forbid")

    machine: str = Field(min_length=1, max_length=128)
    home: str = Field(min_length=1, max_length=4096)
    journal_present: bool
    journal_stage: str | None = Field(default=None, max_length=64)
    normal_release_stage: str | None = Field(default=None, max_length=64)


def cluster_bootstrap_hop_op(payload: BootstrapHopPayload) -> BootstrapHopResult:
    if sys.platform != "linux":
        raise ReleaseRejectedError("restricted hop has no native proof on this platform")
    home = settings.general.ava_home
    machine = unit_local.machine_identity(home)
    request = unit_local.private_unit_reference(payload.hop_request, home)
    logger.info(
        "[cluster_bootstrap_hop] start pid={pid} image={image} request={request}",
        pid=os.getpid(),
        image=payload.artifact_digest,
        request=request.name,
    )
    spawned = spawn_bootstrap_hop(request, artifact_digest=payload.artifact_digest)
    return BootstrapHopResult(
        machine=machine,
        home=str(home),
        session=spawned["session"],
        log=spawned["log"],
    )


def cluster_bootstrap_recovery_read_op(
    payload: BootstrapRecoveryReadPayload,
) -> BootstrapRecoveryReadResult:
    """Report this unit's bootstrap-recovery journal slot, read-only (C-4).

    The exact pre-stop abort's per-unit no-effect proof: the hop child's first
    durable write is this journal (before it, every action was a staged-file
    write or a read-only check), so "absent on every unit" is exactly "no unit
    has acted". A present journal is reported as the fact it is -- readable or
    not -- and the coordinator refuses; nothing here writes, stops or starts
    anything.
    """
    from shared import updater_handoff

    del payload  # no arguments: the question is about this unit's own slot
    home = settings.general.ava_home
    machine = unit_local.machine_identity(home)
    try:
        envelope = updater_handoff.read_bootstrap_recovery()
    except updater_handoff.BootstrapRecoveryInvalidError:
        # Present but unreadable is still a recorded effect.
        return BootstrapRecoveryReadResult(machine=machine, home=str(home), journal_present=True)
    if envelope is None:
        return BootstrapRecoveryReadResult(machine=machine, home=str(home), journal_present=False)
    stage: str | None = None
    normal_stage: str | None = None
    journal_raw = envelope.get("journal")
    if isinstance(journal_raw, dict):
        body = cast("dict[str, object]", journal_raw)
        value = body.get("stage")
        if isinstance(value, str):
            stage = value
        normal_raw = body.get("normal_release")
        if isinstance(normal_raw, dict):
            normal_value = cast("dict[str, object]", normal_raw).get("stage")
            if isinstance(normal_value, str):
                normal_stage = normal_value
    return BootstrapRecoveryReadResult(
        machine=machine,
        home=str(home),
        journal_present=True,
        journal_stage=stage,
        normal_release_stage=normal_stage,
    )


_log = logging.getLogger(__name__)


def spawn_bootstrap_hop(request_path: Path, *, artifact_digest: str) -> dict[str, str]:
    """Trigger a restricted bootstrap hop via a detached `ava-updater` session.

    The session runs the retained candidate image's `--bootstrap-hop` entry
    against `request_path`; every launch mechanic -- the live-session refusal
    pair (before the spawn and again inside the lifecycle mutex), the log
    allocation, the POSIX-only spelling -- lives in `_spawn_updater_entry`
    below.

    Returns {"session": "ava-updater", "log": <path>}.

    Raises:
        ClusterUpdateInProgress: an orchestration session is already live.
        OrchestrationSpawnFailed: the session backend declined to start.
        ReleaseRejectedError: the announced digest names no retained image on
            this unit.
    """
    return _spawn_updater_entry(
        request_path,
        cli_flag="--bootstrap-hop",
        entry_label="hop",
        artifact_digest=artifact_digest,
    )


# The two normal-continuation steps, mapped to the entry flag each runs.
_NORMAL_CONTINUE_CLI_FLAGS = {"drive": "--normal-release", "commit": "--normal-commit"}


def spawn_normal_continue(request_path: Path, *, step: str, artifact_digest: str) -> dict[str, str]:
    """Trigger one normal-continuation step via a detached `ava-updater` session.

    The restricted hop's sibling (task #4129 channel E): `step="drive"` runs the
    retained candidate image's `--normal-release` entry -- the unit's publication
    drive -- and `step="commit"` its `--normal-commit` twin, the post-publication
    commit tail. Every launch mechanic -- the live-session refusal pair, the log
    allocation, the POSIX-only spelling -- is shared with `spawn_bootstrap_hop`
    through `_spawn_updater_entry` below.

    Returns {"session": "ava-updater", "log": <path>}.

    Raises:
        ValueError: `step` is neither "drive" nor "commit".
        ClusterUpdateInProgress: an orchestration session is already live.
        OrchestrationSpawnFailed: the session backend declined to start.
        ReleaseRejectedError: the announced digest names no retained image on
            this unit.
    """
    if step not in _NORMAL_CONTINUE_CLI_FLAGS:
        raise ValueError(f"unknown normal-continuation step: {step!r}")
    return _spawn_updater_entry(
        request_path,
        cli_flag=_NORMAL_CONTINUE_CLI_FLAGS[step],
        entry_label="continuation",
        artifact_digest=artifact_digest,
    )


def _spawn_updater_entry(
    request_path: Path,
    *,
    cli_flag: str,
    entry_label: str,
    artifact_digest: str,
) -> dict[str, str]:
    """Spawn a detached `ava-updater` session that runs one retained-image entry.

    The shared body of the entry triggers (`spawn_bootstrap_hop`,
    `spawn_normal_continue`): the session runs the *retained
    candidate image's* interpreter (`unit_local.candidate_interpreter` --
    strictly resolved under this unit's `releases/`) on
    `-m cli.commands._update_agent_runner <cli_flag> <request>`. The announced
    digest selects an image, never authorizes one -- the entry re-derives every
    binding from the request bytes, and its own compare-and-set
    (`begin_bootstrap_after_dead_owner`) refuses a live or unproven handoff
    owner. Unlike `spawn_update`, nothing is paused and no handoff is seeded:
    the coordinator's exact pre-stop abort (task #4129 C-4) relies on "no unit
    has acted" staying true until the child's first effect.

    Refuses when any orchestration session on this host is already alive (the
    same first line as `spawn_update`), rechecked inside the lifecycle mutex
    before the spawn.

    POSIX-only by construction: the ops handlers refuse any non-Linux platform
    before calling here, so the `native_cmd` slot is unreachable -- it carries
    the POSIX spelling only to satisfy the session backend's two-flavor
    signature.

    Returns {"session": "ava-updater", "log": <path>}.

    Raises:
        ClusterUpdateInProgress: an orchestration session (`ava-updater` /
            rollout / cluster-restart) is already live on this host.
        OrchestrationSpawnFailed: the session backend declined to start the
            entry session.
    """
    # The deploy family is heavy (it reaches `shared.session_backend` /
    # `shared.session_record`); these imports stay method-local so this module's
    # own closure -- the wiring and verdict import its schemas eagerly -- never
    # reaches the session-kill chain. Same discipline as the hop module's.
    from ops import cluster_deploy, deploy_spawn
    from ops.deploy_spawn import ClusterUpdateInProgress

    deploy_spawn.assert_prod_home_has_its_own_checkout()
    updater_sess = shared.cluster.session_name(_UPDATER_SERVICE)
    if cluster_session._has_orchestration_session(updater_sess):
        raise ClusterUpdateInProgress(
            f"orchestration session {updater_sess!r} already exists; an update "
            f"or a {entry_label} is in flight. Wait for it to finish — a hung updater is "
            f"force-reaped automatically — or terminate the pid named in "
            f"$AVA_HOME/run/sessions/{updater_sess}.json if it is hung."
        )
    log_path = cluster_deploy._new_update_log("updater")
    home = settings.general.ava_home
    interpreter = unit_local.candidate_interpreter(home, artifact_digest)
    inner_cmd = (
        f"{{ export AVA_CLI_LOG_NAME=updater; "
        f"if cd {shlex.quote(str(home))}; "
        f"then {shlex.quote(str(interpreter))} -I -B -m cli.commands._update_agent_runner "
        f"{cli_flag} {shlex.quote(str(request_path))}; rc=$?; "
        f"else rc=$?; echo '[updater] cannot enter the unit home; nothing to run'; fi; "
        f'echo "[session-exit] rc=$rc"; }} '
        f"2>&1 | tee -a {shlex.quote(str(log_path))}"
    )
    # Bracket the spawn so a stall inside it is attributable from the log alone
    # (the 2026-08-12 shape; the same pair `spawn_update` writes).
    _log.info("[cluster] spawning %s session %s (log=%s)", entry_label, updater_sess, log_path)
    with shared.ui_update_state.lifecycle_lock():
        live_session = cluster_session.live_orchestration_session()
        if live_session is not None:
            raise ClusterUpdateInProgress(
                f"orchestration session {live_session!r} already exists; "
                f"an update or a {entry_label} is in flight"
            )
        cluster_session._spawn_detached_session(
            updater_sess, shell_cmd=inner_cmd, native_cmd=inner_cmd
        )
    _log.info("[cluster] spawned %s session %s log=%s", entry_label, updater_sess, log_path)
    return {"session": updater_sess, "log": str(log_path)}
