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

from shared import bootstrap, dotenv_boot, paths, runtime_config
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
    assert seen["dials"] == [f"{gateway}/api/bootstrap"]
    # A unit presents its capability's machine API token, never the human
    # cluster secret its `.env` may still carry (none is installed here).
    assert [r["authorization"] for r in _RecordingGateway.requests] == [""]
    assert (default / "run" / "bootstrap-snapshot.json").exists()
    # A runner never takes a database credential from bootstrap: the served
    # URL arrives credential-free.
    assert seen["db_url"] == "postgresql://ava_runner@127.0.0.1:5433/ava"


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


def test_unanchored_checkout_never_decides_to_fetch_nor_dials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with the runner flag and a gateway URL in the environment, an
    unanchored checkout's fetch decision is no — and a caller that skips the
    decision still cannot dial: the transport refuses before any request."""
    monkeypatch.setitem(os.environ, "AVA_MACHINE_SERVE_AGENT_RUNNER", "true")
    monkeypatch.setitem(os.environ, "AVA_GATEWAY_URL", "http://gateway.invalid:8000")
    assert bootstrap.should_fetch_from_gateway() is True
    monkeypatch.setattr(dotenv_boot, "_ANCHORED", False)
    assert bootstrap.should_fetch_from_gateway() is False

    dials: list[object] = []

    def _record(*args: object, **_kwargs: object) -> None:
        dials.append(args)

    monkeypatch.setattr(bootstrap, "dial_get", _record)
    with pytest.raises(bootstrap.BootstrapFetchError, match=r"ava start --worktree"):
        bootstrap.fetch_bootstrap_config("http://gateway.invalid:8000")
    assert dials == []


def test_prod_service_guard_refuses_an_unanchored_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unanchored checkout's home is not `~/.ava`, so the prod-home
    comparison alone would wave it through; it may launch no services at all."""
    monkeypatch.setattr(dotenv_boot, "_ANCHORED", False)
    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path / "scratch")
    err = paths.prod_service_checkout_error(tmp_path / "worktree")
    assert err is not None
    assert "ava start --worktree" in err


def test_settings_free_env_helpers_never_read_the_default_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#3520 P2-1: `runtime_config._ava_home()` (behind `ava config get/set
    --local`) and `bootstrap._serve_flag`'s file fallback (behind
    `config_source_is_local`) used to guess `AVA_HOME env > ~/.ava` on their
    own, independent of the resolved home. The Settings-lite maintenance verbs
    defer `load_ava_env`, so on an unanchored checkout `AVA_HOME` is not yet
    pinned when these run — the guess fell through to a planted runner's
    `~/.ava/.env` (`ava config get --local AVA_MACHINE_NAME` printed its
    machine name) and its `~/.ava/machine_serve_gateway` file."""
    fake_home = tmp_path / "home"
    default = fake_home / ".ava"
    default.mkdir(parents=True)
    (default / ".env").write_text("AVA_MACHINE_NAME=planted-prod-runner\n")
    (default / "machine_serve_gateway").write_text("true\n")
    before = _tree(default)

    monkeypatch.setitem(os.environ, "HOME", str(fake_home))
    monkeypatch.delitem(os.environ, "AVA_HOME", raising=False)
    monkeypatch.delitem(os.environ, "AVA_MACHINE_SERVE_GATEWAY", raising=False)
    monkeypatch.setattr(dotenv_boot, "_checkout_root", lambda: tmp_path / "worktree")

    home = runtime_config._ava_home()

    assert not home.is_relative_to(default), "must resolve away from ~/.ava, not into it"
    assert runtime_config.read_env_aliases() == {}
    assert bootstrap.config_source_is_local() is False
    assert _tree(default) == before, "nothing may be read from or written under ~/.ava"


def test_settings_free_env_helpers_never_create_the_default_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same hazard, from a fake HOME with no `~/.ava` at all: the old fallback's
    `root.mkdir(parents=True, exist_ok=True)` created it (mode 0755) on a bare
    read, even though nothing was ever written to it."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    default = fake_home / ".ava"
    monkeypatch.setitem(os.environ, "HOME", str(fake_home))
    monkeypatch.delitem(os.environ, "AVA_HOME", raising=False)
    monkeypatch.setattr(dotenv_boot, "_checkout_root", lambda: tmp_path / "worktree")

    home = runtime_config._ava_home()

    assert not home.is_relative_to(default)
    assert not default.exists(), "must never create ~/.ava for an unanchored checkout"


def test_first_start_refuses_an_unanchored_checkout_before_fetch_or_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """First start (a remote unit's join included) writes the unit's home; a
    checkout that claims none must name one (`AVA_HOME`) or ask for its own
    (`--worktree`), and refuses before any fetch or write. An AVA_HOME that is
    an unanchored parent's scratch is no claim either."""
    from cli import start_intent

    checkout = tmp_path / "worktree"
    checkout.mkdir()
    monkeypatch.setattr(start_intent, "_checkout", lambda: checkout)
    monkeypatch.delitem(os.environ, "AVA_HOME", raising=False)
    fetched: list[object] = []

    def _no_fetch(*args: object, **_kwargs: object) -> None:
        fetched.append(args)

    monkeypatch.setattr(bootstrap, "fetch_bootstrap_config", _no_fetch)
    with pytest.raises(ValueError, match="requires AVA_HOME or --worktree"):
        start_intent._home(worktree=False)
    scratch = tmp_path / "ava-unanchored-0123456789abcdef"
    monkeypatch.setitem(os.environ, "AVA_HOME", str(scratch))
    with pytest.raises(ValueError, match="requires AVA_HOME or --worktree"):
        start_intent._home(worktree=False)
    assert fetched == []
    assert not scratch.exists()


def test_join_fetches_under_the_home_its_start_claims(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A remote unit's first start fetches before it publishes its home; the
    fetch must already run under that home, or the transport's anchoring gate
    would take a `--worktree` join for an unanchored checkout."""
    from cli import start_intent

    home = tmp_path / ".ava-worktree"
    monkeypatch.delitem(os.environ, "AVA_HOME", raising=False)

    def _credential(*_args: object, **_kwargs: object) -> tuple[None, str]:
        return None, "token"

    monkeypatch.setattr(start_intent, "_join_credential", _credential)
    seen: list[str | None] = []

    def fetch(*_a: object, **_k: object) -> dict[str, str]:
        seen.append(os.environ.get("AVA_HOME"))
        return {}

    monkeypatch.setattr(bootstrap, "fetch_bootstrap_config", fetch)
    start_intent._join({"AVA_GATEWAY_URL": "http://127.0.0.1:8000"}, home, None)
    assert seen == [str(home)]
