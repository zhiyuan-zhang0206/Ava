"""A source preview's root must match its checkout's interpreter, argv and environment."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from base.sessions.env_forwarding import normalize_service_path
from scripts.preview import linux_observer as observer
from scripts.preview import linux_runtime as runtime

# The home stores the admitted, normalized host PATH (admit_service_path). On a
# usrmerge Linux /bin resolves to /usr/bin, so the raw pair is not normalized.
_DECLARED = normalize_service_path("/usr/bin:/bin")


def _expected(run: Path) -> runtime.ExpectedRuntime:
    (run / "home").mkdir()
    (run / "home/.env").write_text(f"AVA_SERVICE_PATH={_DECLARED}\n")
    expected = runtime.expected_runtime(run)
    expected.interpreter.parent.mkdir(parents=True)
    expected.interpreter.write_bytes(b"inert native metadata fixture")
    return expected


@pytest.mark.parametrize(
    "changed",
    [
        None,
        "argv",
        "cwd",
        "executable",
        "AVA_HOME",
        "AVA_HOST_STATE_DIR",
        "VIRTUAL_ENV",
        "AVA_SERVICE_PATH",
        "PATH",
        "PYTHONPATH",
        "isolation",
    ],
)
def test_root_runtime_metadata_and_environment_must_match_even_with_live_birth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str | None
) -> None:
    run = tmp_path.resolve()
    expected = _expected(run)
    environment = expected.environment(run, _DECLARED) | {"PRIVATE_KEY": "never-print-this"}
    native: dict[str, Any] = {
        "argv": expected.argv(run),
        "cwd": str(expected.cwd),
        "executable": str(expected.interpreter.resolve()),
    }
    if changed == "argv":
        native["argv"][0] = "/another/image/venv/bin/python"
    elif changed == "isolation":
        native["argv"].append("--unexpected-option")
    elif changed in {"cwd", "executable"}:
        native[changed] = "/another/image"
    elif changed == "PATH":
        environment[changed] += ":/foreign/extra/bin"
    elif changed is not None:
        environment[changed] = "/another/image"

    class Process:
        def __init__(self, _pid: int):
            pass

        def environ(self) -> dict[str, str]:
            return environment

        def cmdline(self) -> list[str]:
            return native["argv"]

        def cwd(self) -> str:
            return native["cwd"]

        def exe(self) -> str:
            return native["executable"]

    def live(_owner: observer.OwnedProcess) -> bool:
        return True

    monkeypatch.setattr(observer.psutil, "Process", Process)
    monkeypatch.setattr(observer.OwnedProcess, "live", live)
    result: observer.Report = {}
    owner = {"pid": 500, "birth": 100.0, "starttime": 1000}
    if changed is not None:
        with pytest.raises(RuntimeError):
            observer._observe_path(run, owner, result, expected)
    else:
        observer._observe_path(run, owner, result, expected)
        assert result["root_native"]["environment_sha256"] == runtime.environment_digest(
            environment
        )
        assert result["service_path"]["root_path"] == environment["PATH"]
    assert "never-print-this" not in json.dumps(result)
