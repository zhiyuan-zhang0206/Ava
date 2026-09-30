"""Verify one commit of this repository in a fresh Linux container.

    python3 scripts/verify/container.py --ref origin/main
    python3 scripts/verify/container.py --ref HEAD --evidence-root /path/to/evidence

The container is one Ava machine: production layout (`~/.ava`, source at
`~/.ava/source`), native Postgres, Redis and PgBouncer, no systemd, no mounts of the
host's cluster, no published ports. The run resolves the ref to one commit, builds
the image if the provisioning inputs changed, starts a container, clones the commit
into it, builds both dependency trees from that commit's lockfiles, takes the first
`ava start` (gateway and agent-runner on one box), runs the observer, copies the
evidence out, and removes the container with its volumes. The model is scripted;
no provider key is injected, so nothing secret can reach the container. The design
is future/infra/verification-boundaries.md.

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
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

RECIPE = Path(__file__).resolve().parent
REPO = RECIPE.parents[1]
IMAGE = "ava-verify"
CACHE_VOLUME = "ava-verify-cache"
CONTAINER_HOME = "/home/verify"
OBJECTS_MOUNT = "/src/objects"
WORK = f"{CONTAINER_HOME}/verify"

# What the Dockerfile copies into the build; the image tag hashes exactly these.
CONTEXT_FILES = ("scripts/verify/Dockerfile", "scripts/verify/Dockerfile.dockerignore")
CONTEXT_TREES = ("scripts/provision", "scripts/install-cli-tools.sh")

# The start profile (the deleted native preview's, plus a declared time zone): no cron,
# boot or logs jobs, no browser, no remote memory or transfer, no GitHub gate, no
# telemetry export, and the scripted model scenario in place of a provider. No key of
# any kind: nothing secret enters the container.
PROFILE = {
    "AVA_OS_JOBS_ENABLED": "0",
    "AVA_PROVISION_BUILTIN_SCHEDULES": "0",
    "AVA_BROWSER_ENABLED": "0",
    "AVA_MEMORY_KEEP_LOCAL": "1",
    "AVA_CROSS_MACHINE_TRANSFER_BACKEND": "none",
    "AVA_REQUIRE_GITHUB_PR": "0",
    "AVA_LLM_OVERRIDE": "tests.e2e.fakes.scenarios.message_flow:build",
    "AVA_TELEMETRY_OTLP_ENABLED": "0",
    "AVA_TIMEZONE": "UTC",
}
SERVICES = ("gateway", "frontend", "ops", "agent-host")
MACHINE_NAME = "verify"

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

START_ARGV = [
    ".venv/bin/ava",
    "start",
    "--serve-gateway",
    "--serve-agent-runner",
    "--machine-name",
    MACHINE_NAME,
    "--machine-host",
    "127.0.0.1",
    "--config-file",
    f"{WORK}/profile.env",
    *(arg for name in SERVICES for arg in ("--only-service", name)),
]

# Wall-clock bound of each step, seconds. A timed-out step fails the run.
TIMEOUTS = {
    "image-build": 3600,
    "container": 120,
    "prepare-source": 300,
    "python": 1200,
    "frontend": 900,
    "profile-upload": 60,
    "observer-upload": 60,
    "start": 2400,
    "observe": 600,
}


class RunFailedError(RuntimeError):
    """A recipe step failed; the evidence of the steps before it is kept."""


# --------------------------------------------------------------------- the boundary


def refuse_host_state(source: Path) -> None:
    """Nothing of the host's cluster, credentials or container runtime may enter.

    Refuses the home directory, anything that contains it (a parent such as `/Users`
    would expose it), the host cluster and credential directories inside it, and any
    socket (the container runtime's is one, and its path varies by runtime).
    """
    home = Path.home().resolve()
    resolved = source.resolve()
    sealed = [home / name for name in (".ava", ".ssh", ".gnupg", ".aws", ".docker")]
    if (
        home.is_relative_to(resolved)
        or any(resolved.is_relative_to(path) for path in sealed)
        or resolved.name == "docker.sock"
        or resolved.is_socket()
    ):
        raise ValueError(f"refusing to mount host state into the container: {resolved}")


def git_objects_dir(repo: Path) -> Path:
    """The repository's object store: the only host path the container sees."""
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
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


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 — argv list, never a shell
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def resolve_commit(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")


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


@dataclass
class Evidence:
    """One run's directory: a log per step, the observer's JSON, and result.json."""

    root: Path
    result: dict[str, Any]

    def step(self, name: str, argv: list[str], *, stdin: str | None = None) -> None:
        log = self.root / f"{len(self.result['steps']):02d}-{name}.log"
        row: dict[str, Any] = {"name": name, "argv": argv, "log": log.name}
        self.result["steps"].append(row)
        started = time.monotonic()
        try:
            with log.open("w") as out:
                done = subprocess.run(  # noqa: S603 — argv list, never a shell
                    argv,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    input=stdin,
                    stdin=None if stdin is not None else subprocess.DEVNULL,
                    text=True,
                    timeout=TIMEOUTS[name],
                    check=False,
                )
            row["returncode"] = done.returncode
        except subprocess.TimeoutExpired:
            row["returncode"] = "timeout"
        row["seconds"] = round(time.monotonic() - started, 1)
        self.save()
        if row["returncode"] != 0:
            raise RunFailedError(f"step {name} failed ({row['returncode']}): see {log}")

    def save(self) -> None:
        (self.root / "result.json").write_text(json.dumps(self.result, indent=2) + "\n")


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
    profile = "".join(f"{key}={value}\n" for key, value in PROFILE.items())
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
    evidence.step(
        "start", _exec(container, ["bash", "-c", f"cd {source} && {' '.join(START_ARGV)}"])
    )
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
