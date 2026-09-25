"""Early host-wiring and unit-state converge steps."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from cli.commands._converge_spec import ConvergeCtx
from shared.paths import repo_root
from shared.platform_backend import get_backend
from shared.private_storage import converge_private_tree, ensure_private_file

# --- host-wiring steps (no preconditions) ---------------------------------


def _ensure_ava_symlink(ctx: ConvergeCtx) -> None:
    # On Windows the `ava` entry point is `.venv\Scripts\ava.exe`, reached via the
    # uv/venv on PATH (or the install.ps1 shim) — there is no `~/.local/bin/ava`
    # symlink model, and symlink creation needs admin/dev-mode. Skip.
    if not get_backend().supports_ava_symlink():
        return
    target = ctx.repo / ".venv" / "bin" / "ava"
    link = Path.home() / ".local" / "bin" / "ava"
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() and link.readlink() == target:
        return
    link.unlink(missing_ok=True)
    link.symlink_to(target)


_PATH_BEGIN = "# >>> ava path >>>"
_PATH_END = "# <<< ava path <<<"


def _shell_rc_path() -> Path:
    return Path.home() / (".zshrc" if os.environ.get("SHELL", "").endswith("zsh") else ".bashrc")


def _ensure_local_bin_on_path(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    # POSIX shell-rc PATH wiring; Windows uses a different PATH model (the
    # install.ps1 shim / venv Scripts on PATH), so there is no .bashrc to edit.
    if not get_backend().supports_shell_rc():
        return
    local_bin = Path.home() / ".local" / "bin"
    rc = _shell_rc_path()
    line = f'case ":$PATH:" in *":{local_bin}:"*) ;; *) export PATH="{local_bin}:$PATH" ;; esac'
    block = f"{_PATH_BEGIN}\n{line}\n{_PATH_END}"
    existing = rc.read_text() if rc.exists() else ""
    pattern = re.compile(re.escape(_PATH_BEGIN) + r".*?" + re.escape(_PATH_END), re.DOTALL)
    if pattern.search(existing):
        new = pattern.sub(block, existing)
    else:
        sep = "" if existing == "" or existing.endswith("\n") else "\n"
        new = f"{existing}{sep}{block}\n"
    if new != existing:
        rc.write_text(new)


def _ensure_ava_home_dirs(ctx: ConvergeCtx) -> None:
    for sub in ("configs", "secrets"):
        (ctx.ava_home / sub).mkdir(parents=True, exist_ok=True)
    for sub in ("logs", "workspaces", "memory"):
        converge_private_tree(ctx.ava_home / sub)
    marker = ctx.ava_home / "logs" / ".metadata_never_index"
    marker.touch(exist_ok=True)
    ensure_private_file(marker)


def _ensure_prod_editable_pth(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Keep the prod virtualenv anchored to stable, allowlisted source (pth + direct_url)."""
    import shared.cluster_drift
    import shared.editable_install

    source_root = shared.cluster_drift.prod_source_dir()
    if source_root is None:
        return
    repairs = shared.editable_install.repair_editable_install(
        source_root,
        allowed_roots=(Path.home() / "Ava",),
    )
    for repair in repairs:
        print(
            f"  ! poisoned editable install: {repair.path} pointed at "
            f"{repair.poisoned_target!r}; repaired to {repair.source_root}",
            file=sys.stderr,
        )


def _ensure_prod_editable_dir_protection(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Protect editable records and launchers outside the sanctioned write window."""
    if os.name == "nt":
        return
    import shared.cluster_drift
    import shared.editable_install
    from cli.commands.status import _update_in_flight

    if _update_in_flight():
        print("  · prod editable protection skipped: cluster update in flight", file=sys.stderr)
        return
    source_root = shared.cluster_drift.prod_source_dir()
    if source_root is None:
        return
    for directory in shared.editable_install.protected_editable_paths(source_root):
        if directory.stat().st_mode & 0o777 == 0o555:
            continue
        directory.chmod(0o555)


def _ensure_prod_editable_exec_gate(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Fail converge unless the prod venv's console command can import agent code.

    The earlier repair step can restore known pointer and direct-URL records.
    This final gate covers the remaining half-uninstalls, including a missing
    dist-info directory or console script that only a package reinstall can
    restore. An import proof is the final discriminator because a read-only
    site-packages directory can make uv report success after a partial change.
    """

    import shared.cluster_drift
    import shared.editable_install
    from cli.commands._update_uv_sync import run_uv_sync

    source_root = shared.cluster_drift.prod_source_dir()
    if source_root is None:
        return
    allowed_roots = (Path.home() / "Ava",)
    violations = list(
        shared.editable_install.editable_install_violations(
            source_root,
            allowed_roots=allowed_roots,
        )
    )
    violations.extend(shared.editable_install.editable_console_script_violations(source_root))
    if violations:
        print(
            "  ! prod editable install incomplete; attempting one package reinstall recovery",
            file=sys.stderr,
        )
        sync_result = run_uv_sync(source_root, reinstall_package="ava")
        violations = list(
            shared.editable_install.editable_install_violations(
                source_root,
                allowed_roots=allowed_roots,
            )
        )
        violations.extend(shared.editable_install.editable_console_script_violations(source_root))
        if sync_result.returncode != 0:
            violations.append(f"uv sync recovery failed (rc={sync_result.returncode})")
    violations.extend(
        shared.editable_install.editable_import_gate(source_root, allowed_roots=allowed_roots)
    )
    if not violations:
        return
    detail = "\n".join(f"- {violation}" for violation in violations)
    print(f"  ✗ prod editable exec gate failed:\n{detail}", file=sys.stderr)
    raise RuntimeError(
        f"prod editable exec gate failed:\n{detail}\nRun ava cluster update or use the "
        "Manual editable-install recovery write-window recipe in conventions/runbook.md."
    )


def _ensure_pg_binaries_step(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Fetch the vendored relocatable Postgres + inject the pinned pgvector
    extension files (both idempotent — no-ops once the host-level
    `~/.ava/runtime/` tree carries them), so a gateway host needs no
    `brew install postgresql@17` before the data plane comes up and the
    memory-search pgvector backend has its extension binaries."""
    if not get_backend().supports_data_plane():
        return  # Platform uses container-based pg, not vendored binaries
    from shared.runtime_binaries import ensure_pg_binaries, ensure_pgvector

    ensure_pg_binaries()
    ensure_pgvector()


def ensure_local_git_hooks(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Warn when a conventional local checkout's Git hook installation is
    missing or drifted; never repair or block start.

    Runs the stdlib-only checker (``scripts/provision/check_git_hooks.py
    --scan-machine``) from THIS checkout and mirrors its ``WARNING`` lines to
    stderr. A checkout that predates the flag (usage error), a missing script,
    or any subprocess failure degrades to silence — a warn-only assertion must
    never make converge noisy or fatal on its own account.
    """
    script = repo_root() / "scripts" / "provision" / "check_git_hooks.py"
    if not script.exists():
        return
    try:
        result = subprocess.run(
            [sys.executable, str(script), "--scan-machine"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if result.returncode not in (0, 1):
        return  # pre-flag checkout or unexpected failure: stay silent
    for line in result.stdout.splitlines():
        if line.startswith("WARNING:"):
            print(f"  ! hooks: {line.removeprefix('WARNING: ').strip()}", file=sys.stderr)


# --- unit-state steps (need a configured unit) ----------------------------
