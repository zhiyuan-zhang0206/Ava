"""Read-only dependency and import diagnostics for an explicit checkout virtualenv."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from shared import cluster_drift, editable_install, proc, process_env

_log = logging.getLogger("services.healthchecks.prod_venv")

# Each leg has its own deadline, so a hung metadata check cannot skip imports.
_PROBE_TIMEOUT_S = 5.0
_STDERR_TAIL_CHARS = 1000
_IMPORT_SMOKE = """\
import ava, pydantic, psycopg, fastapi
for module in (ava, pydantic, psycopg, fastapi):
    if module.__file__ is None:
        raise ImportError(f"{module.__name__}: hollow package (namespace without __init__.py)")
"""


def _stderr_tail(stderr: str | bytes | None) -> str:
    if stderr is None:
        return ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    # uv's elapsed-time summary changes every round even for identical damage.
    # --quiet also hides incompatibility details, so strip only this summary.
    stderr = re.sub(r"(?m)^Checked \d+ packages? in [^\n]+\n?", "", stderr)
    return stderr[-_STDERR_TAIL_CHARS:].strip()


def _probe(label: str, argv: list[str], env: dict[str, str]) -> str | None:
    """Return a bounded diagnostic for failed execution, including spawn failure."""
    try:
        result = proc.run_bounded(
            argv,
            timeout=_PROBE_TIMEOUT_S,
            cwd=tempfile.gettempdir(),
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        return f"{label} timed out after {_PROBE_TIMEOUT_S:g}s; stderr={_stderr_tail(exc.stderr)!r}"
    except OSError as exc:
        return f"{label} could not start: {exc}"
    if result.returncode:
        return f"{label} failed (rc={result.returncode}); stderr={_stderr_tail(result.stderr)!r}"
    return None


def _violations(*, source_root: Path | None = None) -> tuple[str, ...]:
    if source_root is None:
        source_root = cluster_drift.prod_source_dir()
    if source_root is None:
        return ()
    interpreter = editable_install._venv_python(source_root)
    if interpreter is None:
        return (f"{source_root}: venv python missing",)

    env = process_env.inherited_process_env()
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONPATH", None)
    violations: list[str] = []
    uv = shutil.which("uv")
    if uv is None:
        _log.debug("[prod-venv healthcheck] uv not on PATH; skipping dependency check")
    else:
        failure = _probe("uv pip check", [uv, "pip", "check", "--python", str(interpreter)], env)
        if failure is not None:
            violations.append(failure)
    # -I excludes the parent's PYTHON* settings, cwd and user site-packages;
    # -B prevents this read-only check from writing bytecode into the venv.
    failure = _probe("import smoke", [str(interpreter), "-I", "-B", "-c", _IMPORT_SMOKE], env)
    if failure is not None:
        violations.append(failure)
    return tuple(violations)
