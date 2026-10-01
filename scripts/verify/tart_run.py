"""Verify one commit of this repository in a fresh Tart macOS VM.

    python3 scripts/verify/tart_run.py --ref origin/main
    python3 scripts/verify/tart_run.py --ref HEAD --golden ava-golden --evidence-root DIR

The VM is one Ava machine: production layout (`~/.ava`, source at `~/.ava/source`),
native Postgres, Redis and PgBouncer from Homebrew, the signed permissions helper as
the parent of `ava-root`, and the desktop grants the golden image carries. The run
resolves the ref to one commit, clones the golden image into a throwaway VM, boots it
headless with one read-only share (that commit's history, never the host's object
store), fetches the commit into `~/.ava/source`, makes sure the toolchain is present
(the repository's own provisioning; a no-op in a golden image that has it), builds the
Python tree from that commit's lockfile, runs `ava init` and then the first `ava start`
(gateway and agent-runner on one box; the frontend dependencies and build are the
start's own), runs the observer, copies the evidence out, and deletes the VM. The
model is scripted; no provider key is injected, so nothing secret can reach the VM. The
design is future/infra/verification-boundaries.md.

The host's `~/.ava`, launchd, keychain and TCC are never touched: the only host paths
the run writes are its evidence directory and a temporary export of the commit. At
most two Tart VMs run at once; the run refuses to boot a third. The golden image is
built once, with a human present, by `tart_golden.py`.

This file is host-side and stdlib-only; the observer beside it runs in the guest.
Run trusted branches only: the boundary keeps a mistake away from the host's cluster,
it is not a sandbox against hostile code.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from types import FrameType

RECIPE = Path(__file__).resolve().parent
REPO = RECIPE.parents[1]
sys.path.insert(0, str(REPO))

from scripts.verify.boundary import (  # noqa: E402 — standalone script
    START_ARGV,
    Evidence,
    init_argv,
    profile_text,
    resolve_commit,
)
from scripts.verify.tart_vm import (  # noqa: E402 — standalone script
    GUEST_SOURCE,
    PREPARE_SOURCE,
    PYTHON,
    TOOLCHAIN,
    VM_PREFIX,
    Tart,
    export_commit,
    guest_script,
)

GOLDEN = "ava-golden"
WORK = "$HOME/verify"

KEYCHAIN_GUARD = guest_script(
    'security show-keychain-info "$HOME/Library/Keychains/login.keychain-db"'
)
INIT = guest_script(f'cd "{GUEST_SOURCE}" && {" ".join(init_argv(f"{WORK}/profile.env"))}')
START = guest_script(f'cd "{GUEST_SOURCE}" && {" ".join(START_ARGV)}')
OBSERVE = guest_script(
    f'cd "{GUEST_SOURCE}" && PYTHONPATH="$PWD" .venv/bin/python {WORK}/observe.py {WORK}/observer.json'
)
# What the observer cannot see: the process table (launchd, helper, root, the data plane),
# the disk, and the guest's memory after the start and its frontend build.
SNAPSHOT = guest_script(
    """
echo "== $(date -u +%FT%TZ) $(uptime)"
echo "== processes"
ps -axo pid,ppid,user,etime,command | grep -E 'sbin/launchd$|AvaPermissionsHelper|/\\.ava/|postgres -D|redis-server|pgbouncer|next-server' | grep -v grep || true
echo "== disk"; df -h /
echo "== memory"; sysctl hw.memsize vm.swapusage; memory_pressure | tail -3
"""
)
OBSERVER_JSON = guest_script(f"cat {WORK}/observer.json")
CLUSTER_LOGS = guest_script(
    'cd "$HOME/.ava" && tar -czf - logs $(ls run/ava-root/root.*.log 2>/dev/null) | base64'
)

# Wall-clock bound of each step, seconds. A timed-out step fails the run.
TIMEOUTS = {
    "clone": 600,
    "keychain": 60,
    "prepare-source": 300,
    "toolchain": 1800,
    "python": 1200,
    "profile-upload": 60,
    "observer-upload": 60,
    "init": 120,
    "start": 2400,
    "observe": 600,
    "snapshot": 60,
}


def unpack_logs(data: bytes, destination: Path) -> None:
    """Extract the cluster's logs the guest sent, refusing anything but plain files
    and directories under `destination`."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            target = (root / member.name).resolve()
            if not (member.isfile() or member.isdir()) or not target.is_relative_to(root):
                raise ValueError(f"refusing archive member {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError(f"unreadable archive member {member.name}")
            target.write_bytes(extracted.read())


# ---------------------------------------------------------------------------- run


def run_steps(evidence: Evidence, tart: Tart, vm: str, commit: str) -> None:
    def guest(name: str, script: str, *args: str, stdin: str | None = None) -> None:
        argv = tart.exec_argv(vm, script, *args, stdin=stdin is not None)
        evidence.step(name, argv, stdin=stdin)

    guest("keychain", KEYCHAIN_GUARD)
    guest("prepare-source", PREPARE_SOURCE, commit)
    guest("toolchain", TOOLCHAIN)
    guest("python", PYTHON)
    guest(
        "profile-upload",
        f"install -d {WORK} && cat > {WORK}/profile.env",
        stdin=profile_text(),
    )
    guest(
        "observer-upload",
        f"install -d {WORK} && cat > {WORK}/observe.py",
        stdin=(RECIPE / "observe.py").read_text(),
    )
    guest("init", INIT)
    guest("start", START)
    guest("observe", OBSERVE)
    guest("snapshot", SNAPSHOT)


def collect(evidence: Evidence, tart: Tart, vm: str) -> None:
    """Copy the observer's JSON and the cluster's logs out of the guest, best effort."""
    collected = evidence.result.setdefault("collected", {})
    for name, script in (("observer.json", OBSERVER_JSON), ("cluster-logs", CLUSTER_LOGS)):
        done = subprocess.run(  # noqa: S603 — argv list, never a shell
            tart.exec_argv(vm, script), capture_output=True, text=True, check=False, timeout=120
        )
        collected[name] = done.returncode == 0
        if done.returncode != 0:
            continue
        if name == "observer.json":
            (evidence.root / name).write_text(done.stdout)
        else:
            unpack_logs(base64.b64decode(done.stdout), evidence.root / name)


def run(ref: str, evidence_root: Path, *, repo: Path, golden: str, tart: Tart) -> int:
    commit = resolve_commit(repo, ref)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    evidence = Evidence(
        evidence_root / f"{stamp}-{commit[:8]}",
        {"ref": ref, "commit": commit, "recipe_repo": str(REPO), "steps": [], "result": "running"},
        TIMEOUTS,
    )
    evidence.root.mkdir(parents=True, mode=0o700)
    vm = f"{VM_PREFIX}{commit[:8]}-{uuid.uuid4().hex[:6]}"
    evidence.result["vm"] = vm
    evidence.result["golden"] = golden
    print(f"evidence: {evidence.root}\ncommit: {commit}\nvm: {vm}", flush=True)
    export = Path(tempfile.mkdtemp(prefix="ava-verify-src-"))
    proc: subprocess.Popen[str] | None = None
    failure: BaseException | None = None
    try:
        evidence.result["tart"] = tart.run("--version").stdout.strip()
        tart.require_stopped(golden)
        tart.require_capacity()
        with evidence.action("export"):
            export_commit(repo, commit, export)
        evidence.step("clone", tart.argv("clone", golden, vm))
        with evidence.action("boot"):
            tart.require_capacity()
            proc = tart.boot(vm, evidence.root / "tart-run.log", share=export, graphics=False)
            tart.wait_ready(vm, proc)
        run_steps(evidence, tart, vm, commit)
    except BaseException as error:
        failure = error
        evidence.result["error"] = repr(error)
    finally:
        if proc is not None and proc.poll() is None:
            try:
                collect(evidence, tart, vm)
            except Exception as error:  # evidence is best effort; the VM must still go
                evidence.result["collect_error"] = repr(error)
        tart.halt(vm, proc)
        try:
            evidence.result["removed"] = tart.delete_run_vm(vm)
        except Exception as error:  # the verdict must record a VM that could not be removed
            evidence.result["removed"] = False
            evidence.result["cleanup_error"] = repr(error)
        shutil.rmtree(export, ignore_errors=True)
        observer = evidence.root / "observer.json"
        passed = (
            failure is None
            and observer.is_file()
            and json.loads(observer.read_text())["result"] == "passed"
            and evidence.result["removed"]
        )
        evidence.result["result"] = "passed" if passed else "failed"
        evidence.save()
    print(f"result: {evidence.result['result']} ({evidence.root / 'result.json'})", flush=True)
    if isinstance(failure, (KeyboardInterrupt, SystemExit)):
        raise failure
    return 0 if evidence.result["result"] == "passed" else 1


def _interrupted(_signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(143)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True, help="branch, tag or commit of this repository")
    parser.add_argument("--golden", default=GOLDEN, help="the stopped golden image to clone")
    parser.add_argument(
        "--tart",
        default=shutil.which("tart") or str(Path.home() / ".local/bin/tart"),
        help="the Tart binary (default: tart on PATH, else ~/.local/bin/tart)",
    )
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=Path(tempfile.gettempdir()) / "ava-verify",
        help="parent directory of the per-run evidence directory",
    )
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _interrupted)
    args.evidence_root.mkdir(parents=True, exist_ok=True)
    return run(
        args.ref,
        args.evidence_root.resolve(),
        repo=REPO,
        golden=args.golden,
        tart=Tart(args.tart),
    )


if __name__ == "__main__":
    sys.exit(main())
