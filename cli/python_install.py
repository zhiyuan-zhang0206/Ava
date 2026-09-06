"""Install the canonical lock through a host index without rewriting its pins.

    python cli/python_install.py --locked --inexact [--no-dev] [--verbose]

Only the exported, temporary requirements are passed to the mirror. uv retains
responsibility for lock freshness, dependency groups, markers, artifact hashes,
and isolated editable builds. No second committed lock or resolver is introduced.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

# Fresh installs have no editable package; PYTHONSAFEPATH also excludes cwd.
# Resolve imports from this trusted checkout, including in updater staging trees.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli._python_index import PYPI_INDEX, python_index
from shared.python_lock import violations

# These settings would redirect the target or change the lock's selected pins.
# Transport/cache/TLS environment settings remain inherited; machine files are not replayed.
_IGNORED_ENV = {
    "VIRTUAL_ENV",
    "UV_CONFIG_FILE",
    "UV_PROJECT",
    "UV_PROJECT_ENVIRONMENT",
    "UV_WORKING_DIR",
    "UV_INDEX",
    "UV_EXTRA_INDEX_URL",
    "UV_INDEX_URL",
    "UV_DEFAULT_INDEX",
    "UV_FROZEN",
    "UV_NO_VERIFY_HASHES",
    "UV_OVERRIDE",
    "UV_CONSTRAINT",
    "UV_EXCLUDE",
}


class UvRunner(Protocol):
    """Execute one uv step; the native updater supplies its shared deadline."""

    def __call__(
        self, argv: list[str], repo: Path, env: dict[str, str], *, discard_stdout: bool = False
    ) -> int: ...


def _run(argv: list[str], repo: Path, env: dict[str, str], *, discard_stdout: bool = False) -> int:
    # The standalone installer is foreground; updates inject a bounded uv runner.
    return subprocess.run(  # noqa: S603 — fixed uv argv
        argv,
        cwd=repo,
        env=env,
        check=False,
        stdout=subprocess.DEVNULL if discard_stdout else None,
    ).returncode


def _configured_env(environment: Mapping[str, str], mirror_env: Path | None) -> dict[str, str]:
    """Real single-index environment settings override the entire saved profile."""
    configured = dict(environment)
    if (
        mirror_env is not None
        and mirror_env.is_file()
        and not {"UV_DEFAULT_INDEX", "UV_INDEX_URL"}.intersection(configured)
    ):
        for line in mirror_env.read_text().splitlines():
            key, separator, value = line.partition("=")
            key = key.strip()
            if separator and key in {"UV_DEFAULT_INDEX", "UV_INDEX_URL"} and key not in configured:
                tokens = shlex.split(value, comments=True)
                if len(tokens) != 1:
                    raise ValueError("Invalid Python index entry in mirror.env")
                configured[key] = tokens[0]
    return configured


def install(
    repo: Path,
    *,
    no_dev: bool = False,
    verbose: bool = False,
    interpreter: str | None = None,
    reinstall_package: str | None = None,
    mirror_env: Path | None = None,
    run: UvRunner = _run,
) -> int:
    """Validate first, then install into this checkout's own inexact environment."""
    if (repo / ".venv").is_symlink():
        raise ValueError("The checkout virtualenv must be a real directory, not a symlink")
    errors = violations(repo / "uv.lock")
    if errors:
        raise ValueError("Noncanonical uv.lock; restore the committed PyPI lock before installing")
    configured = _configured_env(os.environ, mirror_env)
    index = python_index(repo, configured)
    env = {key: value for key, value in configured.items() if key not in _IGNORED_ENV}
    flags = (["--no-dev"] if no_dev else []) + (["--verbose"] if verbose else [])
    python_args = ["--python", interpreter] if interpreter else []
    # uv sync can recreate an interpreter-drifted venv before checking freshness.
    # Export validates the manifest without synchronizing the target environment.
    with tempfile.TemporaryDirectory(prefix="ava-python-lock-") as temporary:
        requirements = Path(temporary) / "requirements.txt"
        env["UV_DEFAULT_INDEX"] = PYPI_INDEX
        result = run(
            [
                "uv",
                "export",
                "--locked",
                "--offline",
                "--no-config",
                "--no-emit-project",
                "--no-header",
                "--output-file",
                str(requirements),
                *flags,
                *python_args,
            ],
            repo,
            env,
            discard_stdout=True,
        )
        if result:
            return result
        if index == PYPI_INDEX:
            env["UV_DEFAULT_INDEX"] = PYPI_INDEX
            reinstall = ["--reinstall-package", reinstall_package] if reinstall_package else []
            return run(
                [
                    "uv",
                    "sync",
                    "--locked",
                    "--inexact",
                    "--no-config",
                    *flags,
                    *python_args,
                    *reinstall,
                ],
                repo,
                env,
            )
        target = (
            repo / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        )
        if not target.exists():
            result = run(
                ["uv", "venv", "--no-config", *python_args, str(repo / ".venv")], repo, env
            )
            if result:
                return result
        env["UV_DEFAULT_INDEX"] = index
        common = ["uv", "pip", "install", "--no-config", "--python", str(target), "--no-deps"]
        if verbose:
            common.append("--verbose")
        reinstall = ["--reinstall-package", reinstall_package] if reinstall_package else []
        result = run(
            [*common, "--require-hashes", "--requirements", str(requirements), *reinstall],
            repo,
            env,
        )
        if result:
            return result
        return run([*common, "--editable", str(repo), *reinstall], repo, env)


def main() -> int:
    """Shared dependency-free entry point for install.sh and the bounded updater."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--locked", action="store_true", help="Always enforced")
    parser.add_argument("--inexact", action="store_true", help="Always enforced")
    parser.add_argument("--no-dev", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--python")
    parser.add_argument("--reinstall-package")
    parser.add_argument("--mirror-env", type=Path)
    args = parser.parse_args()
    try:
        return install(
            args.repo.resolve(),
            no_dev=args.no_dev,
            verbose=args.verbose,
            interpreter=args.python,
            reinstall_package=args.reinstall_package,
            mirror_env=args.mirror_env,
        )
    except (ValueError, OSError) as exc:
        print(f"Python installation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
