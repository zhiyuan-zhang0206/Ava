"""An unanchored checkout never reads, dials or writes another cluster's home.

A checkout with no `.ava_home` pointer, no explicit AVA_HOME and not the prod
source (`resolve_ava_home` rule 4) owns no cluster. It used to resolve to the
default home `~/.ava` anyway: importing `shared.config` loaded `~/.ava/.env`
(on a production agent-runner: the serve flags, the gateway URL and the cluster
bearer), sent `GET /api/bootstrap` to that gateway presenting the bearer, and
rewrote `~/.ava/run/bootstrap-snapshot.json`. On 2026-09-27 ad-hoc
`python -c "import ..."` runs and the `types-codegen-fresh` hook did exactly
that from pointerless worktrees on a production runner.

The subprocess tests boot `shared.config` with HOME pointed at a temp dir that
holds a planted `~/.ava` (a pure agent-runner `.env` whose gateway URL is a
recording server here, a `mirror.env`, the serve-flag file). The checkout under
test is simulated by relocating `shared/dotenv_boot.py` into it: the module
anchors on its own `__file__`, so its location IS the checkout. The same harness
run from the simulated prod source (`~/.ava/source`) does fetch and write the
snapshot — the control that proves the negative assertions can see what they
deny.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from shared import bootstrap, dotenv_boot, paths
from shared.dotenv_boot import UNANCHORED_DB_SENTINEL, resolve_ava_home

_REPO = Path(__file__).resolve().parents[2]
_PLANTED_BEARER = "planted-prod-bearer"
_PLANTED_MIRROR = "https://planted-mirror.invalid/simple"

# Relocates the boot module into the simulated checkout, records (or forbids)
# every bootstrap dial, imports shared.config and reports what the boot saw.
# argv: <relocated dotenv_boot.py> <"forbid" | "record">
_CHILD = r"""
import importlib.util, json, os, sys

import shared

spec = importlib.util.spec_from_file_location("shared.dotenv_boot", sys.argv[1])
boot = importlib.util.module_from_spec(spec)
sys.modules["shared.dotenv_boot"] = boot
spec.loader.exec_module(boot)
shared.dotenv_boot = boot

import shared.bootstrap as bootstrap

dials = []
real_dial = bootstrap.dial_get

def dial(*args, **kwargs):
    dials.append(str(args[0]))
    if sys.argv[2] == "forbid":
        raise SystemExit(f"bootstrap fetch attempted: {args[0]}")
    return real_dial(*args, **kwargs)

bootstrap.dial_get = dial

from shared.config import settings

print(json.dumps({
    "anchored": boot.checkout_anchored(),
    "ava_home": os.environ["AVA_HOME"],
    "settings_home": str(settings.general.ava_home),
    "db_url": os.environ.get("AVA_DB_URL"),
    "gateway_url": os.environ.get("AVA_GATEWAY_URL"),
    "bearer": os.environ.get("AVA_CLUSTER_SECRET"),
    "machine_name": os.environ.get("AVA_MACHINE_NAME"),
    "uv_index": os.environ.get("UV_DEFAULT_INDEX"),
    "dials": dials,
}))
"""


class _RecordingGateway(BaseHTTPRequestHandler):
    """The planted gateway: records every request, answers /api/bootstrap."""

    requests: ClassVar[list[dict[str, str]]] = []
    payload: ClassVar[dict[str, str]] = {
        "AVA_DB_URL": "postgresql://ava_runner:fetched@127.0.0.1:5433/ava",
        "AVA_REDIS_URL": "redis://127.0.0.1:6380/0",
    }

    def do_GET(self) -> None:
        _RecordingGateway.requests.append(
            {"path": self.path, "authorization": self.headers.get("Authorization", "")}
        )
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def gateway() -> Iterator[str]:
    _RecordingGateway.requests = []
    server = HTTPServer(("127.0.0.1", 0), _RecordingGateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _plant_default_home(fake_home: Path, gateway_url: str) -> Path:
    """`~/.ava` of a production pure agent-runner, under a temp HOME."""
    default = fake_home / ".ava"
    default.mkdir(parents=True)
    (default / ".env").write_text(
        "AVA_MACHINE_SERVE_AGENT_RUNNER=true\n"
        "AVA_MACHINE_SERVE_GATEWAY=false\n"
        "AVA_MACHINE_NAME=planted-prod-runner\n"
        f"AVA_GATEWAY_URL={gateway_url}\n"
        f"AVA_CLUSTER_SECRET={_PLANTED_BEARER}\n"
    )
    (default / "mirror.env").write_text(f"UV_DEFAULT_INDEX={_PLANTED_MIRROR}\n")
    (default / "machine_serve_agent_runner").write_text("true\n")
    return default


def _checkout(root: Path, pointer: Path | None = None) -> Path:
    """A simulated checkout: the boot module relocated to `<root>/shared/`."""
    (root / "shared").mkdir(parents=True)
    boot = root / "shared" / "dotenv_boot.py"
    shutil.copyfile(_REPO / "shared" / "dotenv_boot.py", boot)
    if pointer is not None:
        (root / ".ava_home").write_text(f"{pointer}\n")
    return boot


def _tree(root: Path) -> dict[str, float]:
    return {str(p.relative_to(root)): p.stat().st_mtime for p in sorted(root.rglob("*"))}


def _boot(boot: Path, fake_home: Path, tmp_path: Path, mode: str, **extra: str) -> dict[str, Any]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("AVA_") and k not in {"UV_DEFAULT_INDEX", "UV_INDEX_URL"}
    }
    scratch_tmp = tmp_path / "tmpdir"
    scratch_tmp.mkdir(exist_ok=True)
    env |= {"HOME": str(fake_home), "TMPDIR": str(scratch_tmp), **extra}
    result = subprocess.run(  # noqa: S603 — fixed argv, repo code, no shell
        [sys.executable, "-c", _CHILD, str(boot), mode],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_unanchored_checkout_boots_bare_and_never_touches_the_default_home(
    tmp_path: Path, gateway: str
) -> None:
    """The 2026-09-27 hazard: no `.env` or `mirror.env` read, no dial, no write
    under `~/.ava`, and the process home is a private scratch — not `~/.ava`."""
    fake_home = tmp_path / "home"
    default = _plant_default_home(fake_home, gateway)
    before = _tree(default)
    boot = _checkout(tmp_path / "worktrees" / "no-pointer")

    seen = _boot(boot, fake_home, tmp_path, "forbid")

    assert seen["dials"] == []
    assert _RecordingGateway.requests == []
    assert _tree(default) == before, "nothing may be written under ~/.ava"
    assert not (default / "run" / "bootstrap-snapshot.json").exists()
    assert seen["anchored"] is False
    home = Path(seen["ava_home"])
    assert not home.is_relative_to(default)
    assert home.is_relative_to(tmp_path / "tmpdir"), "the scratch lives in the temp dir"
    assert seen["settings_home"] == seen["ava_home"]
    assert seen["db_url"] == UNANCHORED_DB_SENTINEL
    # Nothing from the planted `.env` / `mirror.env` reached the process.
    assert seen["bearer"] is None
    assert seen["gateway_url"] is None
    assert seen["machine_name"] is None
    assert seen["uv_index"] is None


def test_prod_source_checkout_still_boots_from_the_default_home(
    tmp_path: Path, gateway: str
) -> None:
    """Control: the prod source (`~/.ava/source`) is the one checkout that owns
    `~/.ava` — it loads that `.env`, fetches with the bearer and writes the
    snapshot. The same harness observing all three is what makes the
    unanchored test's empty observations meaningful."""
    fake_home = tmp_path / "home"
    default = _plant_default_home(fake_home, gateway)
    boot = _checkout(default / "source")

    seen = _boot(boot, fake_home, tmp_path, "record")

    assert seen["anchored"] is True
    assert Path(seen["ava_home"]) == default
    assert seen["bearer"] == _PLANTED_BEARER
    assert seen["uv_index"] == _PLANTED_MIRROR
    assert seen["dials"] == [f"{gateway}/api/bootstrap?role=runner"]
    assert [r["authorization"] for r in _RecordingGateway.requests] == [f"Bearer {_PLANTED_BEARER}"]
    assert (default / "run" / "bootstrap-snapshot.json").exists()
    assert seen["db_url"] == _RecordingGateway.payload["AVA_DB_URL"]


@pytest.mark.parametrize("anchor", ["pointer", "env"])
def test_pointer_and_explicit_home_boot_from_their_own_home(
    anchor: str, tmp_path: Path, gateway: str
) -> None:
    """A `.ava_home` pointer and an explicit AVA_HOME anchor exactly as before:
    the named home's `.env` is the config source, and the default home stays
    untouched."""
    fake_home = tmp_path / "home"
    default = _plant_default_home(fake_home, gateway)
    before = _tree(default)
    own = tmp_path / ".ava-dev"
    own.mkdir()
    (own / ".env").write_text(
        "AVA_MACHINE_SERVE_GATEWAY=true\n"
        "AVA_MACHINE_SERVE_AGENT_RUNNER=true\n"
        "AVA_DB_URL=postgresql://ava@127.0.0.1:15433/ava\n"
        "AVA_REDIS_URL=redis://127.0.0.1:16380/0\n"
    )
    if anchor == "pointer":
        boot = _checkout(tmp_path / "worktrees" / "installed", pointer=own)
        seen = _boot(boot, fake_home, tmp_path, "forbid")
    else:
        boot = _checkout(tmp_path / "worktrees" / "no-pointer")
        seen = _boot(boot, fake_home, tmp_path, "forbid", AVA_HOME=str(own))

    assert seen["anchored"] is True
    assert Path(seen["ava_home"]) == own
    assert seen["db_url"] == "postgresql://ava@127.0.0.1:15433/ava"
    assert seen["dials"] == []
    assert seen["bearer"] is None
    assert _tree(default) == before


# ── in-process: the resolution and every gate that reads it ──


def test_unanchored_resolution_is_a_private_scratch_and_stays_unanchored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rule 4 names a per-process scratch path outside `~/.ava`, and the boot
    pinning it as AVA_HOME does not turn a later re-resolution in the same
    process (preflight gates re-resolve after the pin), or a child inheriting
    a parent's scratch, into an anchored claim. A checkout that claims a home
    of its own still refuses an inherited scratch as a contradiction."""
    monkeypatch.delitem(os.environ, "AVA_HOME", raising=False)
    monkeypatch.delitem(os.environ, "AVA_HOME_OVERRIDE", raising=False)
    monkeypatch.setattr(dotenv_boot, "_checkout_root", lambda: tmp_path)
    home, anchored = resolve_ava_home()
    assert anchored is False
    assert not home.is_relative_to(Path.home() / ".ava")
    assert resolve_ava_home() == (home, False)

    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    assert resolve_ava_home() == (home, False)
    parents = tmp_path / "ava-unanchored-0123456789abcdef"
    monkeypatch.setitem(os.environ, "AVA_HOME", str(parents))
    assert resolve_ava_home() == (parents, False)

    (tmp_path / ".ava_home").write_text(f"{tmp_path / '.ava-dev'}\n")
    with pytest.raises(dotenv_boot.AvaHomeContradictionError):
        resolve_ava_home()


def test_unanchored_checkout_never_decides_to_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with the runner flag and a gateway URL in the environment, an
    unanchored checkout's fetch decision is no."""
    monkeypatch.setitem(os.environ, "AVA_MACHINE_SERVE_AGENT_RUNNER", "true")
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://gateway.invalid:8000")
    assert bootstrap.should_fetch_from_gateway() is True
    monkeypatch.setattr(dotenv_boot, "_ANCHORED", False)
    assert bootstrap.should_fetch_from_gateway() is False


def test_prod_service_guard_refuses_an_unanchored_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unanchored checkout's home is not `~/.ava`, so the prod-home
    comparison alone would wave it through; it may launch no services at all."""
    monkeypatch.setattr(dotenv_boot, "_ANCHORED", False)
    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path / "scratch")
    err = paths.prod_service_checkout_error(tmp_path / "worktree")
    assert err is not None
    assert "install.sh --worktree" in err


def test_enroll_refuses_an_unanchored_checkout_before_fetch_or_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Enroll writes the unit's `.env`; an unanchored checkout has no unit home,
    so the credentials would land in a throwaway scratch. Refuse first."""
    from cli import enroll

    env_path = tmp_path / ".env"
    monkeypatch.setattr(enroll, "AVA_ENV_PATH", env_path)
    monkeypatch.setattr(dotenv_boot, "_ANCHORED", False)
    fetched: list[object] = []

    def _no_fetch(*args: object, **_kwargs: object) -> dict[str, str]:
        fetched.append(args)
        raise AssertionError("enroll fetched from an unanchored checkout")

    monkeypatch.setattr(bootstrap, "fetch_bootstrap_config", _no_fetch)
    rc = enroll.run_enroll(
        ["--gateway", "http://127.0.0.1:8000", "--machine-name", "m", "--machine-host", "127.0.0.1"]
    )
    assert rc == 1
    assert fetched == []
    assert not env_path.exists()
    assert "~/.ava/source" in capsys.readouterr().err
