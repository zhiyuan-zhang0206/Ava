"""`ava cluster update --prepared` hands the request to its executor image (frozen v1).

The running (previous) image reads only the request's envelope, requires the
request to belong to its own home, verifies the executor image in that home's
store against this host and replaces itself with the image's fixed `submit`
entry, settings-free. Everything after the exec is candidate code, so a newer
request shape reaches a candidate that understands it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cli.release_handoff import handoff
from tests.lifecycle.handoff.conftest import Store, entry_argv_tail

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the handoff execs; Windows has no exec")

_REPO = Path(__file__).resolve().parents[3]
# The real CLI entry, with Settings and the command modules unimportable: the
# handoff must reach the exec without either.
_GUARDED_MAIN = """
import importlib.abc
import sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'shared.config', 'cli.commands'}:
            raise AssertionError('forbidden import before the handoff: ' + fullname)
sys.meta_path.insert(0, Deny())
from cli.main import main
raise SystemExit(main(sys.argv[1:]))
"""


def _update(
    store: Store, request: Path, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environment = (
        {**os.environ, "AVA_HOME": str(store.home)} if environment is None else environment
    )
    return subprocess.run(  # noqa: S603 — this interpreter and a literal guard program
        [
            sys.executable,
            "-B",
            "-c",
            _GUARDED_MAIN,
            "cluster",
            "update",
            "--prepared",
            str(request),
        ],
        cwd=_REPO,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _write(tmp_path: Path, document: bytes) -> Path:
    path = tmp_path / "request.json"
    path.write_bytes(document)
    return path


def test_update_execs_the_executor_image_submit_entry(store: Store, tmp_path: Path) -> None:
    request = _write(tmp_path, store.request())
    result = _update(store, request)
    assert result.returncode == 0, result.stderr
    assert result.stdout == '{"ok": true}'
    cwd, home, *argv = store.recorded()
    assert argv == entry_argv_tail("submit", str(request))
    assert Path(cwd) == store.image.cwd.resolve()
    assert home == str(store.home)


def test_an_older_reader_hands_a_newer_request_to_its_candidate(
    store: Store, tmp_path: Path
) -> None:
    """The v1 reader ignores fields and kinds it does not know: a request kind
    added by a later release still reaches the candidate that defines it."""
    document = json.loads(store.request())
    document.update(kind="fleet", units=[{"machine": "unit-b"}], coordinator="http://g:1")
    document.pop("candidate")
    document["executor"]["abi_tag"] = {"os": "a later field"}
    request = _write(tmp_path, json.dumps(document).encode())
    result = _update(store, request)
    assert result.returncode == 0, result.stderr
    assert store.recorded()[2:] == entry_argv_tail("submit", str(request))


def _refusal(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    document: bytes | None,
) -> str:
    def forbidden(*_args: Any) -> None:
        raise AssertionError("a refused handoff must not exec")

    monkeypatch.setattr(os, "execve", forbidden)
    path = tmp_path / "absent.json" if document is None else _write(tmp_path, document)
    assert handoff.run(path) == 2
    assert not (store.record.parent / f"{store.record.name}.argv").exists()
    return capsys.readouterr().err


def _drop(document: bytes, key: str) -> bytes:
    decoded = json.loads(document)
    del decoded[key]
    return json.dumps(decoded).encode()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("missing", "No such file"),
        ("no executor", "no v1 handoff envelope"),
        ("version 2", "no v1 handoff envelope"),
        ("relative home", "no v1 handoff envelope"),
        ("another home", "not to this CLI's home"),
        ("absent image", "release store/generation must be a real directory"),
        ("altered image", "release member hash mismatch"),
    ],
)
def test_the_handoff_refuses_before_any_exec(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: str,
    reason: str,
) -> None:
    monkeypatch.setattr("shared.dotenv_boot.resolve_ava_home", lambda: (store.home, True))
    document: bytes | None = store.request()
    if change == "missing":
        document = None
    elif change == "no executor":
        document = _drop(store.request(), "executor")
    elif change == "version 2":
        document = store.request(version=2)
    elif change == "relative home":
        document = store.request(home="relative/home")
    elif change == "another home":
        other = tmp_path / "other"
        other.mkdir()
        document = store.request(home=str(other.resolve()))
    elif change == "absent image":
        executor = dict(store.executor.model_dump(), artifact_digest="e" * 64)
        document = store.request(executor=executor)
    else:
        member = store.image.root / "venv/lib/python3.12/site-packages/db/schema.sql"
        member.write_bytes(member.read_bytes() + b"-- altered\n")
    assert reason in _refusal(store, tmp_path, monkeypatch, capsys, document)


def test_an_unanchored_cli_refuses_the_handoff(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A checkout that owns no home cannot tell whose request this is."""
    monkeypatch.setattr(
        "shared.dotenv_boot.resolve_ava_home", lambda: (Path.home() / ".ava", False)
    )
    err = _refusal(store, tmp_path, monkeypatch, capsys, store.request())
    assert "owns no home" in err


# ── the previous image's database authority ──────────────────────────────────

_ENDPOINT = "postgresql://ava@127.0.0.1:6433/ava"


def _operator_environment(store: Store, **extra: str) -> dict[str, str]:
    """An operator's shell for this home: no delivered login, no launcher profile."""
    carried = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "AVA_DB_URL",
            "AVA_DB_GENERATION",
            "AVA_API_TOKEN",
            "AVA_PROCESS_PROFILE",
            "AVA_LAUNCHER_PROFILE",
        }
    }
    return {**carried, "AVA_HOME": str(store.home), **extra}


def _born_from(store: Store, checkout: Path, seed: Callable[[Path], Any]) -> Any:
    """A write-generation home born from `checkout`, selecting no release yet:
    that checkout is its admitted runtime."""
    secret = seed(store.home)
    (store.home / ".env").write_text(f"AVA_DB_URL={_ENDPOINT}\n")
    intent = store.home / "start-intent.json"
    intent.write_text(json.dumps({"home": str(store.home), "checkout": str(checkout)}))
    intent.chmod(0o600)
    return secret


def test_an_admitted_cli_hands_its_gateway_login_to_the_executor(
    store: Store, tmp_path: Path, seed_write_generation: Callable[[Path], Any]
) -> None:
    """The handoff builds no Settings, so no boot pass gave this CLI the login an
    admitted `ava` holds. It takes it as the home's admitted runtime and the exec
    carries it, in the environment only, to the executor that is not selected
    yet and so is admitted to nothing itself."""
    gateway = _born_from(store, _REPO, seed_write_generation).roles.gateway
    request = _write(tmp_path, store.request())
    result = _update(store, request, _operator_environment(store))
    assert result.returncode == 0, result.stderr
    assert store.delivered() == (
        f"postgresql://{gateway.name}:{gateway.password}@127.0.0.1:6433/ava",
        "0",
    )
    assert store.recorded()[2:] == entry_argv_tail("submit", str(request))
    assert gateway.password not in result.stdout + result.stderr


def test_a_delivery_the_cli_already_holds_for_its_home_passes_unchanged(
    store: Store, tmp_path: Path, seed_write_generation: Callable[[Path], Any]
) -> None:
    _born_from(store, _REPO, seed_write_generation)
    held = "postgresql://ava_g7_gateway:held@127.0.0.1:6433/ava"
    environment = _operator_environment(store, AVA_DB_URL=held, AVA_DB_GENERATION="7")
    result = _update(store, _write(tmp_path, store.request()), environment)
    assert result.returncode == 0, result.stderr
    assert store.delivered() == (held, "7")


@pytest.mark.parametrize(
    ("runtime", "reason"),
    [
        ("foreign", "not the home's source checkout"),
        ("launched", "only the root launcher delivers database logins"),
    ],
)
def test_a_cli_without_database_authority_refuses_before_any_exec(
    store: Store,
    tmp_path: Path,
    seed_write_generation: Callable[[Path], Any],
    runtime: str,
    reason: str,
) -> None:
    """A stale runtime, or a CLI a launcher started without a delivery, holds no
    login to hand over: the handoff refuses by name instead of starting an
    executor whose first dial would fail."""
    checkout = tmp_path / "another-checkout" if runtime == "foreign" else _REPO
    _born_from(store, checkout, seed_write_generation)
    profile = {"AVA_PROCESS_PROFILE": "agent"} if runtime == "launched" else {}
    result = _update(
        store, _write(tmp_path, store.request()), _operator_environment(store, **profile)
    )
    assert result.returncode == 2
    assert "release update refused: " in result.stderr and reason in result.stderr
    assert not (store.record.parent / f"{store.record.name}.argv").exists()
