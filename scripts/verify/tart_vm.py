"""The Tart driver the macOS verification recipes share.

`tart_golden.py` builds the one-time golden image and `tart_run.py` verifies a commit in
a throwaway clone of it; both drive Tart through `Tart` and put the commit into the
guest through `export_commit`. This module is where the macOS boundary's rules live:

- at most two VMs run at once (the macOS license's two guests, which the kernel
  enforces; whether Linux guests count is not established, so every running Tart VM
  counts), checked before a VM is booted;
- the only host path a guest sees is a freshly built bare repository holding that one
  commit's history, mounted read-only and never the host's object store (a share holding
  objects hard-linked to the host's was read with "Permission denied" in the guest, while a
  fresh-fetch export read cleanly);
- no clipboard, audio or USB passthrough between host and guest;
- the host's cluster, credentials, keychain and VM store never enter a guest
  (`boundary.refuse_host_state`), and the host's own state is never an argument.

Stdlib-only and host-side. The design is future/infra/engineering/verification-boundaries.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts.verify.boundary import (  # noqa: E402 — standalone script
    RunFailedError,
    git,
    refuse_host_state,
)

# The macOS license allows two guests on one Apple host; the golden image counts while it runs.
VM_LIMIT = 2
# Every VM a verification run creates carries this prefix, and only such a VM is ever deleted.
VM_PREFIX = "ava-verify-"
SHARE_NAME = "src"
EXPORT_BRANCH = "verify"
# macOS guests mount a share under /Volumes/My Shared Files/<name>.
GUEST_SHARE = f"/Volumes/My Shared Files/{SHARE_NAME}"
# `tart exec` starts commands with a bare PATH; uv lives in ~/.local/bin, Homebrew in /opt/homebrew.
GUEST_PATH = 'export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"'
GUEST_SOURCE = "$HOME/.ava/source"
READY_TIMEOUT_S = 180

# The commit goes into `~/.ava/source` as a standalone clone with no remote, the way the
# container recipe does it. The golden image carries no cluster, so a home that was
# already initialized is refused rather than overwritten.
PREPARE_SOURCE = f"""
set -euo pipefail
test ! -e "$HOME/.ava/start-intent.json"
install -d -m 700 "$HOME/.ava"
rm -rf "$HOME/.ava/source"
git init -q "$HOME/.ava/source"
cd "$HOME/.ava/source"
git fetch -q --no-tags "{GUEST_SHARE}/source.git" refs/heads/{EXPORT_BRANCH}
git -c advice.detachedHead=false checkout -q --detach "$1"
git rev-parse HEAD
"""


def guest_script(body: str) -> str:
    """A guest shell script: strict mode and the PATH `tart exec` lacks, then the body."""
    return f"set -euo pipefail\n{GUEST_PATH}\n{body}"


# The repository's own macOS provisioning (Homebrew postgresql@17, redis@8.2, pgbouncer and
# pgvector; the pinned uv). Installing what a golden image already has is a no-op, so a
# commit that moves a pin is verified against its own provisioning. Node comes from the image.
TOOLCHAIN = guest_script(
    f'cd "{GUEST_SOURCE}"\nbash scripts/provision/database.sh\nbash scripts/provision/toolchain.sh'
)
PYTHON = guest_script(f'cd "{GUEST_SOURCE}" && uv sync --locked')


def export_commit(repo: Path, commit: str, destination: Path) -> Path:
    """A bare repository under `destination` holding only that commit's history.

    Built by a fresh fetch, so every file in it is new and unlinked from the host's own
    object store, and the host repository's config (remote URLs) and other branches
    stay out. Returns the directory to mount; the guest fetches `refs/heads/verify`.
    """
    bare = destination / "source.git"
    subprocess.run(  # noqa: S603 — argv list, never a shell
        ["git", "init", "--bare", "-q", str(bare)], check=True, capture_output=True
    )
    git(bare, "fetch", "--no-tags", "-q", str(repo), f"{commit}:refs/heads/{EXPORT_BRANCH}")
    refuse_host_state(destination)
    return destination


class Tart:
    """One Tart binary. With `dry_run`, commands are printed and never run."""

    def __init__(self, binary: str, *, dry_run: bool = False) -> None:
        self.binary = binary
        self.dry_run = dry_run

    def argv(self, *args: str) -> list[str]:
        return [self.binary, *args]

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if self.dry_run:
            print(f"[dry-run] {' '.join(self.argv(*args))}", flush=True)
            return subprocess.CompletedProcess(self.argv(*args), 0, "", "")
        return subprocess.run(  # noqa: S603 — argv list, never a shell
            self.argv(*args), capture_output=True, text=True, check=check
        )

    # ------------------------------------------------------------------- inventory

    def vms(self) -> list[dict[str, Any]]:
        """The local VMs (images pulled from a registry are not VMs)."""
        if self.dry_run:
            return []
        listed = json.loads(self.run("list", "--format", "json").stdout)
        return [row for row in listed if row["Source"] == "local"]

    def find(self, name: str) -> dict[str, Any] | None:
        return next((row for row in self.vms() if row["Name"] == name), None)

    def require_capacity(self) -> None:
        """Refuse to boot a VM when the two the license allows are already running."""
        if self.dry_run:
            return
        running = sorted(row["Name"] for row in self.vms() if row["Running"])
        if len(running) >= VM_LIMIT:
            raise RunFailedError(
                f"{len(running)} Tart VMs are running ({', '.join(running)}); at most "
                f"{VM_LIMIT} may run at once, so another one is not started"
            )

    def require_stopped(self, name: str) -> None:
        """A clone is taken only from a VM that exists locally and is stopped."""
        row = self.find(name)
        if row is None:
            raise RunFailedError(f"no local Tart VM named {name}")
        if row["Running"]:
            raise RunFailedError(f"{name} is running; clone only from a stopped image")

    def require_absent(self, name: str) -> None:
        if self.find(name) is not None:
            raise RunFailedError(f"a Tart VM named {name} exists; delete it yourself first")

    # ------------------------------------------------------------------------ boot

    def boot_argv(self, name: str, *, share: Path | None, graphics: bool) -> list[str]:
        """`tart run` with no clipboard, audio or USB passthrough and, at most, one
        read-only share: the commit's export."""
        argv = ["run", "--no-audio", "--no-clipboard", "--no-usb-accessories"]
        if not graphics:
            argv.append("--no-graphics")
        if share is not None:
            refuse_host_state(share)
            argv.append(f"--dir={SHARE_NAME}:{share}:ro")
        return self.argv(*argv, name)

    def boot(
        self, name: str, log: Path, *, share: Path | None, graphics: bool
    ) -> subprocess.Popen[str] | None:
        """Start the VM in the background; `tart run` stays in the foreground of its own
        process, so it is a child kept for `stop`."""
        argv = self.boot_argv(name, share=share, graphics=graphics)
        if self.dry_run:
            print(f"[dry-run] {' '.join(argv)}  (in the background)", flush=True)
            return None
        with log.open("w") as out:
            return subprocess.Popen(  # noqa: S603 — argv list, never a shell
                argv,
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )

    def exec_argv(self, name: str, script: str, *args: str, stdin: bool = False) -> list[str]:
        """Run a shell script in the guest through the guest agent (`$1`... are `args`)."""
        return self.argv("exec", *(["-i"] if stdin else []), name, "bash", "-c", script, "_", *args)

    def wait_ready(self, name: str, proc: subprocess.Popen[str] | None) -> None:
        """Wait until the guest agent answers; fail at once if `tart run` has exited."""
        if self.dry_run:
            print(f"[dry-run] wait until `tart exec {name} true` answers", flush=True)
            return
        if proc is None:
            raise RunFailedError(f"{name} was not booted by this run")
        deadline = time.monotonic() + READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RunFailedError(f"`tart run` for {name} exited early ({proc.returncode})")
            if self.run("exec", name, "true", check=False).returncode == 0:
                return
            time.sleep(3)
        raise RunFailedError(f"the guest agent of {name} did not answer in {READY_TIMEOUT_S}s")

    # -------------------------------------------------------------------- shutdown

    def halt(self, name: str, proc: subprocess.Popen[str] | None, *, grace_s: int = 60) -> None:
        """Stop a running VM and wait for its `tart run` to exit."""
        if proc is None or proc.poll() is not None:
            return
        self.run("stop", name, check=False)
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def shutdown_guest(self, name: str, proc: subprocess.Popen[str] | None) -> None:
        """Shut the guest down from inside and wait for `tart run` to exit; `tart exec`
        reports a dropped transport at that moment, which is normal."""
        self.run("exec", name, "sudo", "/sbin/shutdown", "-h", "now", check=False)
        if proc is None:
            return
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            self.halt(name, proc)

    def delete_run_vm(self, name: str) -> bool:
        """Delete a VM this recipe created; true when it is gone afterwards.

        Only a name with the run prefix is ever deleted, so the golden image and the
        VMs of earlier experiments cannot be reached from here.
        """
        if not name.startswith(VM_PREFIX):
            raise ValueError(f"refusing to delete {name}: not a verification run VM")
        if self.find(name) is not None:
            self.run("delete", name, check=False)
        return self.find(name) is None
