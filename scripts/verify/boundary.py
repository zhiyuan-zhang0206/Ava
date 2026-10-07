"""Host-side primitives shared by the verification recipes.

A verification boundary (a Linux container, a Tart macOS VM) is born from one commit,
runs one cluster through `ava init` and `ava start`, is observed, and is destroyed. What
every recipe shares lives here: the start profile (a scripted model, no key of any
kind), the init and start argv, the refusal to let host state in, the commit
resolution, and the evidence directory with its bounded, logged steps. The design is
future/infra/engineering/verification-boundaries.md.

Stdlib-only and host-side, like the recipes that import it.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The start profile (the deleted native preview's, plus a declared time zone): no cron,
# boot or logs jobs, no browser, no remote memory or transfer, no GitHub gate, no
# telemetry export, and the scripted model scenario in place of a provider. No key of
# any kind: nothing secret enters a boundary.
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

# `ava init` records the machine's identity; `ava start` takes only the service selection.
START_ARGV = [
    ".venv/bin/ava",
    "start",
    *(arg for name in SERVICES for arg in ("--only-service", name)),
]


def profile_text() -> str:
    return "".join(f"{key}={value}\n" for key, value in PROFILE.items())


def init_argv(profile_path: str) -> list[str]:
    return [
        ".venv/bin/ava",
        "init",
        "--serve-gateway",
        "--serve-agent-runner",
        "--machine-name",
        MACHINE_NAME,
        "--machine-host",
        "127.0.0.1",
        "--config-file",
        profile_path,
    ]


class RunFailedError(RuntimeError):
    """A recipe step failed; the evidence of the steps before it is kept."""


# --------------------------------------------------------------------- the boundary


def refuse_host_state(source: Path) -> None:
    """Nothing of the host's cluster, credentials, keychain or VM store may enter.

    Refuses the home directory, anything that contains it (a parent such as `/Users`
    would expose it), the host cluster, credential, keychain (`Library`) and Tart
    (`.tart`) directories inside it, and any socket (the container runtime's is one,
    and its path varies by runtime).
    """
    home = Path.home().resolve()
    resolved = source.resolve()
    sealed = [
        home / name for name in (".ava", ".ssh", ".gnupg", ".aws", ".docker", ".tart", "Library")
    ]
    if (
        home.is_relative_to(resolved)
        or any(resolved.is_relative_to(path) for path in sealed)
        or resolved.name == "docker.sock"
        or resolved.is_socket()
    ):
        raise ValueError(f"refusing to mount host state into the boundary: {resolved}")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 — argv list, never a shell
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def resolve_commit(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")


# ------------------------------------------------------------------------- evidence


@dataclass
class Evidence:
    """One run's directory: a log per step, the observer's JSON, and result.json."""

    root: Path
    result: dict[str, Any]
    timeouts: dict[str, int]

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
                    timeout=self.timeouts[name],
                    check=False,
                )
            row["returncode"] = done.returncode
        except subprocess.TimeoutExpired:
            row["returncode"] = "timeout"
        row["seconds"] = round(time.monotonic() - started, 1)
        self.save()
        if row["returncode"] != 0:
            raise RunFailedError(f"step {name} failed ({row['returncode']}): see {log}")

    @contextmanager
    def action(self, name: str) -> Generator[None]:
        """A step done in this process rather than by a command (booting a VM): timed and
        recorded like one, and an error in it fails the run."""
        row: dict[str, Any] = {"name": name, "argv": None, "log": None}
        self.result["steps"].append(row)
        started = time.monotonic()
        try:
            yield
            row["returncode"] = 0
        except BaseException as error:
            row["returncode"] = "error"
            row["error"] = repr(error)
            raise
        finally:
            row["seconds"] = round(time.monotonic() - started, 1)
            self.save()

    def save(self) -> None:
        (self.root / "result.json").write_text(json.dumps(self.result, indent=2) + "\n")
