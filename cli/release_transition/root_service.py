"""Use the existing Linux root boot unit, separate from the finite updater.

The stage subprocesses (preflight, observation) are platform-neutral; macOS
starts root through the persistent home helper instead: root_macos.py.
"""

from __future__ import annotations

import os
from pathlib import Path

from base.deploy.release.runtime_release import VerifiedRelease
from base.host.system.boot_unit import BootStartAction, BootUnitContext, install, unit_name
from cli.release_transition.journal import Operation
from cli.release_transition.request import ReleaseRef


def _context(home: Path, image: VerifiedRelease) -> BootUnitContext:
    import grp
    import pwd

    from base.paths import host_state_dir

    account = pwd.getpwuid(os.getuid())
    return BootUnitContext(
        home=home,
        repo=image.cwd,
        user=account.pw_name,
        group=grp.getgrgid(account.pw_gid).gr_name,
        home_dir=Path(account.pw_dir),
        host_state_dir=host_state_dir(),
    )


def _environment(context: BootUnitContext) -> tuple[tuple[str, str], ...]:
    return stage_environment(context.home, context.host_state_dir, context.home_dir)


def stage_environment(
    home: Path, host_state_dir: Path, home_dir: Path
) -> tuple[tuple[str, str], ...]:
    """The complete environment of every stage action, on every platform.

    The service PATH declaration is read from the home's persisted settings.
    No caller credential, editable import path, or shell startup file travels.
    On Linux the start action runs inside the boot unit, whose manager adds
    variables of its own (`User=` sets USER and LOGNAME); the root launch
    digest binds only declared launch inputs (`launch_input_keys`), so the
    observer still derives the digest the start recorded.
    """
    return (
        ("HOME", str(home_dir)),
        ("AVA_HOME", str(home)),
        ("AVA_HOST_STATE_DIR", str(host_state_dir)),
        ("PATH", os.defpath),
    )


def start(operation: Operation, image: VerifiedRelease) -> None:
    from base.host.system.boot_unit import manager_properties, privileged

    context = _context(Path(operation.request.home), image)
    action = BootStartAction(
        image.module_argv(
            "cli.release_transition.stage", "--operation", str(operation.request.path)
        ),
        image.cwd,
        _environment(context),
        restart_on_failure=False,
    )
    install(context=context, action=action)
    properties = manager_properties(context.home)
    if properties["ControlPID"] != "0":
        raise RuntimeError("the root boot unit already has an unresolved start/stop action")
    if properties["MainPID"] != "0":
        observe(operation, image)
        return
    result = privileged(
        ["/usr/bin/systemctl", "start", unit_name(context.home)],
        timeout=660,
    )
    if result.returncode:
        raise RuntimeError(f"root start action failed: {result.stderr.strip()}")
    observe(operation, image)


def observe(operation: Operation, image: VerifiedRelease) -> None:
    from base.host.proc import run_bounded

    context = _context(Path(operation.request.home), image)
    result = run_bounded(
        list(
            image.module_argv(
                "cli.release_transition.stage",
                "--operation",
                str(operation.request.path),
                "--observe",
            )
        ),
        cwd=image.cwd,
        env=dict(_environment(context)),
        timeout=120,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(f"selected root observation failed: {result.stderr[-2048:]}")


def preflight(operation: Operation, image: VerifiedRelease, *, previous: bool) -> None:
    """Both complete startup rosters must be preparable before draining work."""
    from base.host.proc import run_bounded

    arguments = ["--operation", str(operation.request.path), "--preflight"]
    if previous:
        arguments.append("--previous")
    context = _context(Path(operation.request.home), image)
    result = run_bounded(
        list(image.module_argv("cli.release_transition.stage", *arguments)),
        cwd=image.cwd,
        env=dict(_environment(context)),
        timeout=120,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(f"release roster preflight failed: {result.stderr[-2048:]}")


def restore_boot(operation: Operation, image: VerifiedRelease) -> None:
    """A completed operation must not become a permanent boot command."""
    reference = operation.reference
    install_steady(Path(operation.request.home), reference, image)


def install_steady(home: Path, reference: ReleaseRef, image: VerifiedRelease) -> None:
    """Install one pinned ordinary boot action without fabricating an operation."""
    if reference.verify(home) != image:
        raise ValueError("steady boot image differs from its captured reference")
    context = _context(home, image)
    action = BootStartAction(
        image.module_argv(
            "cli.release_transition.boot",
            "--home",
            str(home),
            "--artifact",
            reference.artifact_digest,
            "--manifest",
            reference.manifest_digest,
            "--schema",
            reference.schema_digest,
            "--commit",
            reference.source_commit,
        ),
        image.cwd,
        _environment(context),
    )
    install(context=context, action=action)
