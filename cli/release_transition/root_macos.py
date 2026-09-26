"""Start ava-root on a retained image through the persistent macOS home helper.

On macOS the ordinary start boundary never spawns root itself: it persists the
image's seed and asks the home helper's keeper to spawn root as the helper's own
child, outside the finite executor job. The executor therefore runs the selected
image's stage action as a finite tool in its job group and journals which
helper and keeper epoch produced which root. Neither lifetime can end the other:
the job's group closure never reaches the helper's session, and a root stop
never reaches the job.

Every helper interaction is authenticated by the kernel socket peer, the running
image's stable signature and the operation's recorded helper artifact. The
keeper restarts a root that exits unexpectedly; the journaled root birth and
keeper restart count make such a replacement visible, so it can never pass as
the observed start (Linux starts the operation action without restart).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Literal, cast

import psutil

from cli.release_transition.journal import Journal, Operation
from cli.release_transition.launchd_custody import Birth, DarwinLaunch, RootCustody
from cli.release_transition.root_service import observe as observe_selected
from cli.release_transition.root_service import stage_environment
from services.permissions_helper import client, finite_artifact
from shared.native_process.ownership import OwnedProcess
from shared.runtime_release import VerifiedRelease
from shared.verified_file import regular_bytes

# The ordinary start's own readiness bound, as for the Linux unit start action.
START_TIMEOUT_S = 660
_ROOT_MODULE = "services.ava_root"
_SEED_FIELDS = ("argv", "cwd", "run_dir", "stdout", "stderr")


def _release(operation: Operation) -> tuple[DarwinLaunch, Literal["candidate", "previous"]]:
    if operation.pitr is not None or operation.launch is None or operation.direction is None:
        raise RuntimeError("helper root start admits only a launched macOS release")
    return DarwinLaunch.model_validate(operation.launch), operation.direction


def verified_helper(operation: Operation) -> OwnedProcess:
    """This home's helper: kernel socket peer, signed running image, recorded artifact."""
    launch, _direction = _release(operation)
    home = Path(operation.request.home)
    helper, executable = finite_artifact.home_helper(home)
    if finite_artifact.signed_artifact(executable, home) != launch.helper:
        raise RuntimeError("home helper differs from the operation's signed helper artifact")
    if helper.pid in {parent.pid for parent in psutil.Process().parents()}:
        raise RuntimeError("the home helper cannot be an ancestor of the release executor")
    if not helper.live():
        raise RuntimeError("home helper changed during authentication")
    return helper


def _keeper(helper: OwnedProcess) -> client.RootStatus:
    """The keeper's state, bracketed by the kernel peer of this exact helper birth."""
    from shared import paths

    socket = paths.permissions_helper_socket()
    status = client.root_status(sock_path=socket)
    _reply, peer = client.ping_peer(sock_path=socket)
    if peer != helper.pid or not helper.live():
        raise RuntimeError("home helper changed while its root keeper was read")
    return status


def _live_root(helper: OwnedProcess, keeper: client.RootStatus) -> OwnedProcess:
    """The root answering this home's control socket is the keeper's live direct child."""
    from shared.paths import root_run_dir
    from shared.root_control.client import root_process

    root = root_process()
    if root is None:
        raise RuntimeError("the selected root is not live")
    try:
        parent = psutil.Process(root.pid).ppid()
    except psutil.Error as exc:
        raise RuntimeError("the selected root is not observable") from exc
    if (
        parent != helper.pid
        or keeper.get("pid") != root.pid
        or keeper["state"] != "running"
        or keeper["stop_requested"]
        or keeper.get("run_dir") != str(root_run_dir())
        or not root.live()
    ):
        raise RuntimeError("the selected root is not the home helper keeper's live child")
    return root


def _require_pinned(image: VerifiedRelease, keeper: client.RootStatus, root: OwnedProcess) -> None:
    """Both spawn sources name this image: the keeper's seed and the persisted seed file.

    The keeper respawns an unexpectedly exited root from its held seed, and a
    restarted helper (or a new login) from ``seed.json``; the live root must be
    the exact argv and working directory both would launch.
    """
    from shared.paths import root_run_dir

    run_dir = root_run_dir()
    reported = keeper.get("seed")
    if reported is None:
        raise RuntimeError("home helper does not report its retained root seed")
    try:
        document: object = json.loads(regular_bytes(run_dir / "seed.json"))
        process = psutil.Process(root.pid)
        live = {"argv": process.cmdline(), "cwd": process.cwd()}
    except (OSError, ValueError, psutil.Error) as exc:
        raise RuntimeError("the retained root seed or live root launch is unreadable") from exc
    # A non-object seed file has no fields, so it can never equal the report.
    fields = cast("dict[str, object]", document) if isinstance(document, dict) else {}
    persisted = {key: fields.get(key) for key in _SEED_FIELDS}
    prefix = list(image.module_argv(_ROOT_MODULE))
    argv = reported["argv"]
    if (
        persisted != dict(reported)
        or argv[: len(prefix)] != prefix
        or reported["cwd"] != str(image.cwd)
        or reported["run_dir"] != str(run_dir)
        or live != {"argv": argv, "cwd": reported["cwd"]}
        or not root.live()
    ):
        raise RuntimeError("the retained root seed is not pinned to the selected image")


def _data_plane() -> dict[str, OwnedProcess]:
    """Live local data-plane births; the whole local data plane must be up."""
    from cli.commands._maintenance_data_plane import capture_custody

    receipt = capture_custody(30)
    return {name: owner.identity for name, owner in receipt.owners().items()}


def _require_same_data_plane(
    before: dict[str, OwnedProcess], after: dict[str, OwnedProcess]
) -> None:
    # A same-schema release keeps the data plane. A data process born by the
    # start action would live outside both the job group and root custody.
    if before.keys() != after.keys() or any(
        not before[key].same_birth(after[key]) for key in before
    ):
        raise RuntimeError("the release start changed native data-plane births; custody refused")


def _run_start_action(operation: Operation, image: VerifiedRelease) -> None:
    """The selected image's ordinary start, as a finite tool of the executor job."""
    import pwd

    from shared.proc import run_bounded

    request = operation.request
    account = pwd.getpwuid(os.getuid())
    environment = stage_environment(
        Path(request.home), Path(request.registry), Path(account.pw_dir)
    )
    argv = image.module_argv("cli.release_transition.stage", "--operation", str(request.path))
    try:
        result = run_bounded(
            list(argv),
            cwd=image.cwd,
            env=dict(environment),
            timeout=START_TIMEOUT_S,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("root start action exceeded its bound; its tree was closed") from exc
    if result.returncode:
        raise RuntimeError(f"root start action failed: {result.stderr[-2048:]}")


def start(journal: Journal, image: VerifiedRelease) -> None:
    """Start root on the selected image through the home helper, once per direction."""
    operation = journal.operation
    _launch, direction = _release(operation)
    helper = verified_helper(operation)
    keeper = _keeper(helper)
    prior = None if operation.root is None else RootCustody.model_validate(operation.root)
    if prior is not None and prior.direction == direction and prior.root is not None:
        # The effect completed in an earlier attempt: observe only its root.
        observe(operation, image)
        return
    if prior is None or prior.direction != direction:
        stopped = keeper["state"] == "stopped" and keeper["stop_requested"]
        unseeded = keeper["state"] == "unseeded" and not keeper["seeded"]
        if keeper.get("pid") is not None or not (stopped or unseeded):
            raise RuntimeError("home helper keeps or may restart a root before this start")
    journal.root_intent(
        RootCustody(
            direction=direction, helper=Birth.of(helper), restarts=keeper["restarts"]
        ).model_dump(mode="json")
    )
    before = _data_plane()
    _run_start_action(journal.operation, image)
    _require_same_data_plane(before, _data_plane())
    helper = verified_helper(journal.operation)
    keeper = _keeper(helper)
    root = _live_root(helper, keeper)
    journal.root_started(
        RootCustody(
            direction=direction,
            helper=Birth.of(helper),
            restarts=keeper["restarts"],
            root=Birth.of(root),
        ).model_dump(mode="json")
    )
    observe(journal.operation, image)


def require_custody(operation: Operation, image: VerifiedRelease) -> None:
    """The journaled start root still runs, unreplaced, under its helper and pinned seed."""
    _launch, direction = _release(operation)
    if operation.root is None:
        raise RuntimeError("the selected root has no journaled start receipt")
    custody = RootCustody.model_validate(operation.root)
    if custody.direction != direction or custody.root is None:
        raise RuntimeError("the selected root has no journaled start receipt")
    helper = verified_helper(operation)
    if Birth.of(helper) != custody.helper:
        raise RuntimeError("the home helper was replaced after the release start")
    keeper = _keeper(helper)
    root = _live_root(helper, keeper)
    if Birth.of(root) != custody.root or keeper["restarts"] != custody.restarts:
        raise RuntimeError("the keeper replaced the started root; the start is not observed")
    _require_pinned(image, keeper, root)


def observe(operation: Operation, image: VerifiedRelease) -> None:
    """Fresh readiness of the selected image, then its exact journaled root custody."""
    observe_selected(operation, image)
    require_custody(operation, image)


def restore_boot(operation: Operation, image: VerifiedRelease) -> None:
    """The keeper's pinned seed is the steady state; there is no boot unit to install."""
    require_custody(operation, image)


def require_stopped(operation: Operation) -> None:
    """After stop, the keeper holds no root and cannot restart one without a new seed."""
    keeper = _keeper(verified_helper(operation))
    stopped = keeper["state"] == "stopped" and keeper["stop_requested"]
    unseeded = keeper["state"] == "unseeded" and not keeper["seeded"]
    if keeper.get("pid") is not None or not (stopped or unseeded):
        raise RuntimeError("home helper keeper may still restart the stopped root")
