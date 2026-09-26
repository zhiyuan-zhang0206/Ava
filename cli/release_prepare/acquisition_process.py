"""Bound acquisition commands to private output, configuration and tool caches."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from cli.release_prepare.acquisition_models import CommandEvidence
from cli.release_prepare.models import FileInput
from shared.atomic_io import write_text_atomic
from shared.posix_command import run_owned_command
from shared.runtime_release import ReleaseRejectedError, file_sha256


@contextmanager
def _preserve_failure(primary: BaseException) -> Generator[None]:
    """Keep diagnostic I/O failure secondary to the command's actual failure."""
    try:
        yield
    except OSError as recording:
        primary.add_note(f"could not retain acquisition command diagnostic: {recording}")


class Commands:
    def __init__(self, work: Path, uv: Path) -> None:
        self.work = work
        self.uv = uv
        self.evidence: list[CommandEvidence] = []
        for name in ("home", "cache", "tmp", "logs", "python", "npm-cache"):
            (work / name).mkdir(mode=0o700)
        self.environment = {
            "PATH": os.defpath,
            "HOME": str(work / "home"),
            "TMPDIR": str(work / "tmp"),
            "UV_CACHE_DIR": str(work / "cache"),
            "UV_PYTHON_INSTALL_DIR": str(work / "python"),
            "UV_PYTHON_INSTALL_BIN": "0",
            "UV_PYTHON_INSTALL_REGISTRY": "0",
            "UV_NO_CONFIG": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "NEXT_TELEMETRY_DISABLED": "1",
            "NPM_CONFIG_CACHE": str(work / "npm-cache"),
            "NPM_CONFIG_USERCONFIG": os.devnull,
            "NPM_CONFIG_GLOBALCONFIG": str(work / "home/empty-npmrc"),
        }
        (work / "home/empty-npmrc").write_text("")

    def run(
        self,
        argv: list[str],
        cwd: Path,
        *,
        timeout: int = 900,
        environment: dict[str, str] | None = None,
    ) -> str:
        started_at = datetime.now(UTC)
        started = time.monotonic()
        progress: dict[str, object] = {
            "index": len(self.evidence),
            "argv": argv,
            "cwd": str(cwd),
            "started_at": started_at.isoformat(),
            "status": "running",
        }
        self._progress(progress)
        try:
            result = run_owned_command(
                argv,
                cwd=cwd,
                env=self.environment | (environment or {}),
                timeout=timeout,
                temporary=self.work / "tmp",
            )
        except subprocess.TimeoutExpired as exc:

            def decoded(value: str | bytes | None) -> str:
                return value.decode(errors="replace") if isinstance(value, bytes) else value or ""

            with _preserve_failure(exc):
                self._record(
                    argv,
                    cwd,
                    decoded(exc.stdout) + decoded(exc.stderr),
                    None,
                    started_at=started_at,
                    elapsed_seconds=time.monotonic() - started,
                    timed_out=True,
                )
            with _preserve_failure(exc):
                self._progress(
                    progress
                    | {"status": "timed-out", "elapsed_seconds": time.monotonic() - started}
                )
            raise
        except BaseException as exc:
            # Native custody errors and interruption remain failures, never guessed
            # exit codes or absence. The finite process owner retains custody proof.
            with _preserve_failure(exc):
                self._progress(
                    progress
                    | {
                        "status": "interrupted-or-failed",
                        "error_type": type(exc).__name__,
                        "elapsed_seconds": time.monotonic() - started,
                    }
                )
            raise
        output = self._record(
            argv,
            cwd,
            result.stdout + result.stderr,
            result.returncode,
            started_at=started_at,
            elapsed_seconds=time.monotonic() - started,
        )
        self._progress(
            progress
            | {
                "status": "passed" if result.returncode == 0 else "failed",
                "returncode": result.returncode,
                "elapsed_seconds": time.monotonic() - started,
            }
        )
        if result.returncode:
            raise ReleaseRejectedError(
                f"acquisition command failed: {Path(argv[0]).name} rc={result.returncode}; {output}"
            )
        return result.stdout.strip()

    def _progress(self, value: dict[str, object]) -> None:
        """A live diagnostic view, never authority to select, resume or reap."""
        write_text_atomic(
            self.work / "command-progress.json",
            json.dumps(value, sort_keys=True) + "\n",
            mode=0o600,
        )

    def _record(
        self,
        argv: list[str],
        cwd: Path,
        text: str,
        returncode: int | None,
        *,
        started_at: datetime,
        elapsed_seconds: float,
        timed_out: bool = False,
    ) -> Path:
        output = self.work / "logs" / f"{len(self.evidence):03d}.log"
        with output.open("x") as stream:
            stream.write(text)
        self.evidence.append(
            CommandEvidence(
                argv=tuple(argv),
                cwd=cwd,
                output=FileInput(path=output, digest=file_sha256(output)),
                returncode=returncode,
                started_at=started_at,
                elapsed_seconds=elapsed_seconds,
                timed_out=timed_out,
            )
        )
        return output

    def package(self, *argv: str, cwd: Path, timeout: int = 900) -> str:
        return self.run([str(self.uv), "--no-config", *argv], cwd, timeout=timeout)
