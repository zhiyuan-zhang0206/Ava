"""Verify one commit of this repository in a fresh Linux container.

    python3 scripts/verify/container.py --ref origin/main
    python3 scripts/verify/container.py --ref HEAD --evidence-root /path/to/evidence

The container is one Ava machine: production layout (`~/.ava`, source at
`~/.ava/source`), native Postgres, Redis and PgBouncer, no systemd, no mounts of the
host's cluster, no published ports. The run resolves the ref to one commit, builds
the image if the provisioning inputs changed, starts a container, clones the commit
into it, builds both dependency trees from that commit's lockfiles, runs `ava init`
and then the first `ava start` (gateway and agent-runner on one box), runs the observer, copies the
evidence out, and removes the container with its volumes. The model is scripted;
no provider key is injected, so nothing secret can reach the container. The design
is future/infra/engineering/verification-boundaries.md.

This file is host-side and stdlib-only; the observer beside it runs in the container.
Run trusted branches only: the boundary keeps a mistake away from the host's cluster,
it is not a sandbox against hostile code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import suppress
from pathlib import Path
from types import FrameType

RECIPE = Path(__file__).resolve().parent
REPO = RECIPE.parents[1]
sys.path.insert(0, str(REPO))

from scripts.verify.boundary import (  # noqa: E402 — standalone script
    START_ARGV,
    Evidence,
    git,
    init_argv,
    profile_text,
    refuse_host_state,
    resolve_commit,
)

IMAGE = "ava-verify"
CACHE_VOLUME = "ava-verify-cache"
CONTAINER_HOME = "/home/verify"
OBJECTS_MOUNT = "/src/objects"
WORK = f"{CONTAINER_HOME}/verify"

# What the Dockerfile copies into the build; the image tag hashes exactly these.
CONTEXT_FILES = ("scripts/verify/Dockerfile", "scripts/verify/Dockerfile.dockerignore")
CONTEXT_TREES = ("scripts/provision", "scripts/install-cli-tools.sh")

PREPARE_SOURCE = """
set -euo pipefail
install -d -m 700 "$HOME/.ava"
git init -q "$HOME/.ava/source"
cd "$HOME/.ava/source"
echo {objects} > .git/objects/info/alternates
git -c advice.detachedHead=false checkout -q --detach "$1"
git repack -a -d -q
rm .git/objects/info/alternates
git rev-parse HEAD
"""

INIT_ARGV = init_argv(f"{WORK}/profile.env")

# Wall-clock bound of each step, seconds. A timed-out step fails the run.
TIMEOUTS = {
    "image-build": 3600,
    "container": 120,
    "prepare-source": 300,
    "python": 1200,
    "frontend": 900,
    "profile-upload": 60,
    "observer-upload": 60,
    "init": 120,
    "start": 2400,
    "observe": 600,
}


# --------------------------------------------------------------------- the boundary


def git_objects_dir(repo: Path) -> Path:
    """The repository's object store: the only host path the container sees."""
    common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    objects = Path(common) / "objects"
    if not objects.is_dir():
        raise ValueError(f"{repo} has no object store at {objects}")
    refuse_host_state(objects)
    return objects


def docker_run_argv(name: str, image: str, objects: Path, memory: str, shm: str) -> list[str]:
    """The one `docker run`: no published port, no environment, no socket, no SSH key,
    no host home; the object store read-only; a cache volume that holds no cluster."""
    refuse_host_state(objects)
    return [
        "docker",
        "run",
        "-d",
        "--init",
        "--name",
        name,
        "--memory",
        memory,
        "--shm-size",
        shm,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--mount",
        f"type=bind,src={objects},dst={OBJECTS_MOUNT},ro",
        "-v",
        f"{CACHE_VOLUME}:{CONTAINER_HOME}/.cache",
        image,
    ]


# ------------------------------------------------------------------------- helpers


def context_digest(repo: Path) -> str:
    """Hash of every byte the image build can see (working-tree files, relative names)."""
    files = [repo / name for name in CONTEXT_FILES]
    for name in CONTEXT_TREES:
        path = repo / name
        files.extend(sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path])
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(repo)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:12]


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — argv list, never a shell
        ["docker", *args], capture_output=True, text=True, check=check
    )


def _exec(container: str, argv: list[str], *, stdin: bool = False) -> list[str]:
    return ["docker", "exec", *(["-i"] if stdin else []), container, *argv]


# ---------------------------------------------------------------------------- run


def build_image(evidence: Evidence) -> str:
    """The image for the current provisioning inputs; built once, reused after."""
    tag = f"{IMAGE}:{context_digest(REPO)}"
    if _docker("image", "inspect", tag, check=False).returncode == 0:
        evidence.result["image"] = {"tag": tag, "built": False}
    else:
        evidence.step(
            "image-build",
            [
                "docker",
                "build",
                "--progress=plain",
                "-f",
                str(RECIPE / "Dockerfile"),
                "-t",
                tag,
                str(REPO),
            ],
        )
        evidence.result["image"] = {"tag": tag, "built": True}
    info = json.loads(_docker("image", "inspect", tag).stdout)[0]
    base = _docker("image", "inspect", "ubuntu:24.04", "--format", "{{index .RepoDigests 0}}")
    evidence.result["image"] |= {
        "id": info["Id"],
        "architecture": info["Architecture"],
        "size_bytes": info["Size"],
        "base": base.stdout.strip(),
    }
    return tag


def run_steps(evidence: Evidence, container: str, commit: str) -> None:
    source = f"{CONTAINER_HOME}/.ava/source"
    profile = profile_text()
    observer = (RECIPE / "observe.py").read_text()
    observe = f"cd {source} && PYTHONPATH={source} .venv/bin/python {WORK}/observe.py {WORK}/observer.json"
    prepare = PREPARE_SOURCE.format(objects=OBJECTS_MOUNT)
    evidence.step("prepare-source", _exec(container, ["bash", "-c", prepare, "_", commit]))
    evidence.step("python", _exec(container, ["bash", "-c", f"cd {source} && uv sync --locked"]))
    evidence.step("frontend", _exec(container, ["bash", "-c", f"cd {source}/ui/web && npm ci"]))
    for name, script, content in (
        ("profile-upload", f"install -d {WORK} && cat > {WORK}/profile.env", profile),
        ("observer-upload", f"cat > {WORK}/observe.py", observer),
    ):
        evidence.step(name, _exec(container, ["bash", "-c", script], stdin=True), stdin=content)
    for name, argv in (("init", INIT_ARGV), ("start", START_ARGV)):
        evidence.step(name, _exec(container, ["bash", "-c", f"cd {source} && {' '.join(argv)}"]))
    evidence.step("observe", _exec(container, ["bash", "-c", observe]))


def collect(evidence: Evidence, container: str) -> None:
    """Copy the observer's JSON and the cluster's logs out, best effort."""
    for name, path in (
        ("observer.json", f"{WORK}/observer.json"),
        ("cluster-logs", f"{CONTAINER_HOME}/.ava/logs"),
    ):
        copied = subprocess.run(  # noqa: S603 — argv list, never a shell
            ["docker", "cp", f"{container}:{path}", str(evidence.root / name)],
            capture_output=True,
            text=True,
            check=False,
        )
        evidence.result.setdefault("collected", {})[name] = copied.returncode == 0


def run(ref: str, evidence_root: Path, memory: str, shm: str) -> int:
    commit = resolve_commit(REPO, ref)
    objects = git_objects_dir(REPO)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    evidence = Evidence(
        evidence_root / f"{stamp}-{commit[:8]}",
        {"ref": ref, "commit": commit, "recipe_repo": str(REPO), "steps": [], "result": "running"},
        TIMEOUTS,
    )
    evidence.root.mkdir(parents=True, mode=0o700)
    container = f"ava-verify-{commit[:8]}-{uuid.uuid4().hex[:6]}"
    evidence.result["container"] = container
    print(f"evidence: {evidence.root}\ncommit: {commit}", flush=True)
    failure: BaseException | None = None
    try:
        image = build_image(evidence)
        evidence.step("container", docker_run_argv(container, image, objects, memory, shm))
        run_steps(evidence, container, commit)
    except BaseException as error:
        failure = error
        evidence.result["error"] = repr(error)
    finally:
        collect(evidence, container)
        with suppress(subprocess.CalledProcessError):
            _docker("rm", "-f", "-v", container)
        evidence.result["removed"] = not _docker(
            "ps", "-a", "-q", "--filter", f"name=^{container}$"
        ).stdout.strip()
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
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=Path(tempfile.gettempdir()) / "ava-verify",
        help="parent directory of the per-run evidence directory",
    )
    parser.add_argument("--memory", default="8g", help="container memory limit")
    parser.add_argument("--shm-size", default="1g", help="container /dev/shm size")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _interrupted)
    args.evidence_root.mkdir(parents=True, exist_ok=True)
    return run(args.ref, args.evidence_root.resolve(), args.memory, args.shm_size)


if __name__ == "__main__":
    sys.exit(main())
