"""Interpreter paths for the currently imported code, never a moving selector.

Editable development keeps its checkout venv. A wheel consumes the interpreter
that loaded it, not an imaginary site-packages/.venv. Release verification and
activation remain the deployment owner's responsibility; this is not admission.
"""

from __future__ import annotations

import sys
from pathlib import Path

from shared.platform import IS_WINDOWS

_PREFIX = Path(sys.prefix).resolve()
WHEEL_RUNTIME = Path(__file__).resolve().is_relative_to(_PREFIX)


def runtime_venv(*, checkout: Path | None = None) -> Path:
    """Return the current runtime environment or an explicitly targeted checkout."""
    if checkout is not None:
        return checkout / ".venv"
    if WHEEL_RUNTIME:
        if sys.prefix == sys.base_prefix:
            raise RuntimeError("installed Ava requires an isolated virtual environment")
        return _PREFIX
    from shared.paths import repo_root

    return repo_root() / ".venv"


def runtime_python() -> Path:
    """Absolute Python path anchored to the loaded wheel or development checkout."""
    return runtime_venv() / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def runtime_frontend_dir() -> Path:
    """Bind the frontend to the same loaded generation; admission verifies assets."""
    if not WHEEL_RUNTIME:
        raise RuntimeError("retained frontend paths require wheel runtime")
    return runtime_venv().parent / "frontend"


def runtime_plugins_dir() -> Path:
    """Read-only external plugin root of the already loaded generation."""
    if not WHEEL_RUNTIME:
        raise RuntimeError("retained plugin paths require wheel runtime")
    return runtime_venv().parent / "plugins"


def runtime_otel_binary() -> Path:
    """Resolve only the loaded image's collector, never mutable home storage."""
    if not WHEEL_RUNTIME:
        raise RuntimeError("retained collector paths require wheel runtime")
    return (
        runtime_venv().parent
        / "otel"
        / ("otelcol-contrib.exe" if IS_WINDOWS else "otelcol-contrib")
    )
