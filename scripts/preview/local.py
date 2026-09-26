"""Run a local Git revision in a disposable, unseeded Ava cluster.

This controller uses only the standard library. Target code runs in the target
checkout's own interpreter; it never imports the invoking checkout's settings.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from contextlib import suppress
from pathlib import Path
from types import FrameType
from typing import TextIO

ENV_KEYS = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "TMPDIR", "TZ")
PROFILE = {
    "AVA_OS_JOBS_ENABLED": "0",
    "AVA_PROVISION_BUILTIN_SCHEDULES": "0",
    "AVA_BROWSER_ENABLED": "0",
    "AVA_MEMORY_KEEP_LOCAL": "1",
    "AVA_CROSS_MACHINE_TRANSFER_BACKEND": "none",
    "AVA_REQUIRE_GITHUB_PR": "0",
    "AVA_LLM_OVERRIDE": "tests.e2e.fakes.scenarios.message_flow:build",
    "AVA_TELEMETRY_OTLP_ENABLED": "0",
}


def clean_env() -> dict[str, str]:
    """Do not inherit production addresses, provider keys or Python overrides."""
    return {key: os.environ[key] for key in ENV_KEYS if key in os.environ} | PROFILE


def write_json(path: Path, value: object) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.replace(path)


def resolve_ref(repo: Path, ref: str) -> str:
    return subprocess.check_output(  # noqa: S603 — Git ref is a positional argv, never shell code
        ["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        text=True,
        env=clean_env(),
    ).strip()


def run_command(
    argv: list[str], cwd: Path, env: dict[str, str], output: TextIO, timeout: float
) -> None:
    """Cancel the foreground build tree on timeout or interruption."""
    process = subprocess.Popen(  # noqa: S603 — explicit local tooling argv, no shell
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        result = process.wait(timeout=timeout)
    except BaseException:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise
    if result:
        raise subprocess.CalledProcessError(result, argv)


class Preview:
    """One persisted run; logs and data survive teardown for inspection."""

    def __init__(self, run: Path):
        self.run = run.resolve()
        self.source = self.run / "source"
        self.home = self.run / "home"
        self.manifest = self.run / "run.json"
        self.data = json.loads(self.manifest.read_text())
        if self.data["format"] != "ava-local-preview-v2" or self.data["run"] != str(self.run):
            raise ValueError("Not an owned preview run")
        if self.home.is_symlink() or self.source.is_symlink():
            raise ValueError("Preview paths must not be symlinks")
        self.env = clean_env() | {
            "AVA_HOME": str(self.home),
            "AVA_CLUSTER_REGISTRY": str(self.run / "clusters.json"),
        }

    def save(self) -> None:
        write_json(self.manifest, self.data)

    def command(
        self, name: str, argv: list[str], *, cwd: Path | None = None, timeout: float = 900
    ) -> None:
        index = len(self.data["steps"])
        log = self.run / f"{index:02d}-{name}.log"
        step: dict[str, object] = {"name": name, "argv": argv, "log": str(log), "result": "running"}
        self.data["steps"].append(step)
        self.save()
        print(f"{name}: {log}", flush=True)
        started = time.monotonic()
        try:
            with log.open("w") as output:
                run_command(argv, cwd or self.source, self.env, output, timeout)
            step["result"] = "passed"
        except BaseException:
            step["result"] = "failed"
            raise
        finally:
            step["seconds"] = round(time.monotonic() - started, 3)
            self.save()

    def runtime(self, action: str) -> None:
        self.command(
            action,
            [
                str(self.source / ".venv/bin/python"),
                str(self.run / "runtime.py"),
                str(self.run),
                action,
            ],
        )

    def cli(self, name: str, args: list[str]) -> None:
        self.command(name, [str(self.source / ".venv/bin/python"), "-m", "cli.main", *args])

    def assert_checkout(self) -> None:
        if resolve_ref(self.source, "HEAD") != self.data["commit"]:
            raise ValueError("Preview checkout changed since preparation")
        pointer = self.source / ".ava_home"
        if pointer.exists() and Path(pointer.read_text().strip()).resolve() != self.home:
            raise ValueError("Preview checkout points to a different home")

    def prepare(self) -> None:
        self.command(
            "checkout",
            [
                "git",
                "-C",
                self.data["repo"],
                "worktree",
                "add",
                "--detach",
                str(self.source),
                self.data["commit"],
            ],
            cwd=self.run,
        )
        self.command("python", ["uv", "venv", "--python", "3.12", ".venv"])
        self.command(
            "python-dependencies",
            [
                str(self.source / ".venv/bin/python"),
                "cli/python_install.py",
                "--locked",
                "--inexact",
            ],
        )
        self.command("frontend-dependencies", ["npm", "ci"], cwd=self.source / "ui/web")
        # Last before start: the origin names the port block first start allocates.
        self.runtime("allow-browser-origin")
        self.data["state"] = "prepared"
        self.save()

    def start(self) -> None:
        self.assert_checkout()
        flags = [arg for name in self.data["services"] for arg in ("--only-service", name)]
        self.cli(
            "start",
            [
                "start",
                "--worktree",
                "--machine-host",
                "127.0.0.1",
                "--config-file",
                str(self.run / "profile.env"),
                *flags,
            ],
        )
        self.runtime("describe")
        config = json.loads((self.run / "config.json").read_text())
        self.data["state"] = "ready"
        self.save()
        print(f"Preview: {config['frontend_url']}\nGateway: {config['gateway_url']}", flush=True)

    def stop(self) -> None:
        self.assert_checkout()
        self.data["cleanup"] = "running"
        self.save()
        try:
            # Start persists identity before any native effect. Its normal stop
            # owns cleanup even when startup failed; preview never kills daemons.
            if (self.home / ".env").exists():
                self.cli("stop", ["stop", "-y", "--stop-browser"])
                self.cli("destroy", ["cluster", "destroy", "--path", str(self.home)])
            self.runtime("verify-stopped")
            self.data["cleanup"] = "passed"
            self.data["state"] = "stopped"
        except BaseException:
            self.data["cleanup"] = "failed"
            raise
        finally:
            self.save()


def create(repo: Path, ref: str, root: Path) -> Preview:
    commit = resolve_ref(repo, ref)
    root.mkdir(parents=True, exist_ok=True)
    run = root.resolve() / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    run.mkdir(mode=0o700)
    profile = PROFILE | {"AVA_PERMISSIONS_HELPER_ARTIFACT_DIR": str(run / "helper-artifact")}
    profile_path = run / "profile.env"
    profile_path.write_text("".join(f"{key}={value}\n" for key, value in profile.items()))
    profile_path.chmod(0o600)
    write_json(
        run / "run.json",
        {
            "format": "ava-local-preview-v2",
            "run": str(run),
            "repo": str(repo.resolve()),
            "requested_ref": ref,
            "profile": profile,
            "services": ["gateway", "frontend", "ops", "agent-host"],
            "commit": commit,
            "controller_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "adapter_sha256": hashlib.sha256(
                Path(__file__).with_name("runtime.py").read_bytes()
            ).hexdigest(),
            "state": "created",
            "cleanup": "pending",
            "scope": "Source checkout, private data plane, core services, scripted LLM with real execution",
            "excludes": [
                "release image update/rollback",
                "multi-machine behavior",
                "real LLM providers",
                "browser/computer tools",
                "production promotion and CI approval",
            ],
            "steps": [],
        },
    )
    shutil.copyfile(Path(__file__).with_name("runtime.py"), run / "runtime.py")
    print(f"Run: {run}\nCommit: {commit}", flush=True)
    return Preview(run)


def run_preview(preview: Preview, *, keep: bool) -> None:
    try:
        preview.prepare()
        preview.start()
        preview.runtime("smoke")
        preview.runtime("check")
        preview.data["verification"] = "passed"
        preview.save()
    except BaseException:
        preview.data["verification"] = "failed"
        preview.save()
        raise
    finally:
        if not keep or preview.data.get("verification") != "passed":
            if (preview.source / ".venv/bin/python").exists():
                preview.stop()
            else:
                preview.data["cleanup"] = "not-started"
                preview.save()


def interrupted(_signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(143)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run", help="Resolve a local ref once, prepare, start, verify and stop")
    run.add_argument("--ref", required=True, help="Local branch, fetched remote ref or commit")
    run.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    run.add_argument("--root", type=Path, default=Path.home() / ".ava-previews")
    run.add_argument("--keep", action="store_true", help="Keep a successful preview running")
    for action in ("check", "stop"):
        command = sub.add_parser(action)
        command.add_argument("run", type=Path)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupted)
    preview = create(args.repo, args.ref, args.root) if args.action == "run" else Preview(args.run)
    with (preview.run / "operation.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "run":
            run_preview(preview, keep=args.keep)
        elif args.action == "stop":
            preview.stop()
        else:
            preview.assert_checkout()
            preview.runtime("check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
