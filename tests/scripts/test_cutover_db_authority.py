"""The one-time Redis cutover against a real, home-owned Redis.

A pre-cutover home is reproduced exactly as the previous bring-up left it: empty
Redis credentials in `.env`, a redis.conf without `requirepass`, and a `nopass`
runtime ACL user. The cutover must convert it, keep its data, refuse ambiguous
state before any effect, and be a verified no-op when repeated.
"""

from __future__ import annotations

import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import psutil
import pytest
import redis
from dotenv import dotenv_values
from redis.backoff import NoBackoff
from redis.retry import Retry

from cli.commands import _cluster_instance as instance
from scripts import cutover_db_authority as cutover
from shared.config import settings
from tests._containers import _free_port, redis_server

_OPEN_ENV = {"AVA_REDIS_ADMIN_PASSWORD": "", "AVA_REDIS_PASSWORD": "", "AVA_CLUSTER_SECRET": ""}
_LANDED_RUNTIME = "landed-runtime"
_FOREIGN_ADMIN = "someone-else"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A registered-looking home whose Redis directory is killed on teardown."""
    path = tmp_path / "home"
    path.mkdir(mode=0o700)
    monkeypatch.setattr(settings.general, "ava_home", str(path))
    try:
        yield path.resolve()
    finally:
        data = (path / "redis").resolve()
        for process in psutil.process_iter(["name"]):
            try:
                if process.info["name"] == "redis-server" and Path(process.cwd()) == data:
                    process.send_signal(signal.SIGKILL)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue


def _write_env(home: Path, port: int, values: dict[str, str], url: str | None = None) -> None:
    body = {"AVA_REDIS_URL": url or f"redis://ava@127.0.0.1:{port}/0", **values}
    (home / ".env").write_text("".join(f"{key}={value}\n" for key, value in body.items()))


def _wait(port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"redis did not listen on {port}")


def _start_legacy_redis(home: Path, port: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """The previous no-secret bring-up: no requirepass and a `nopass` ACL user."""
    monkeypatch.setattr(settings.data_plane, "redis_url", f"redis://ava@127.0.0.1:{port}/0")
    data = home / "redis"
    data.mkdir(mode=0o700)
    conf = data / "redis.conf"
    conf.write_text("save 900 1\nsave 300 10\nsave 60 10000\n")
    subprocess.run(  # noqa: S603 — the home-owned redis-server binary
        [
            instance._redis_server_bin(),
            str(conf),
            "--daemonize",
            "yes",
            "--port",
            str(port),
            "--bind",
            "127.0.0.1",
            "--protected-mode",
            "no",
            "--dir",
            str(data),
            "--logfile",
            str(data / "redis.log"),
        ],
        check=True,
        capture_output=True,
    )
    _wait(port)
    with redis.Redis(port=port) as client:
        client.execute_command(  # pyright: ignore[reportUnknownMemberType] — redis command stubs
            "ACL", "SETUSER", "ava", "on", "resetpass", "nopass", "~*", "&ava:*", "+@all"
        )
        client.set("continuation", "kept")  # pyright: ignore[reportUnknownMemberType] — redis command stubs


def _pid(port: int, password: str) -> int:
    with redis.Redis(port=port, password=password) as client:
        return int(client.info("server")["process_id"])  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType] — redis command stubs


def _legacy_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    port = _free_port()
    _write_env(home, port, _OPEN_ENV)
    _start_legacy_redis(home, port, monkeypatch)
    return port


def _assert_converted(home: Path, port: int) -> str:
    """Prove the converted posture; return the admin password."""
    env = dotenv_values(home / ".env")
    admin, runtime = env["AVA_REDIS_ADMIN_PASSWORD"] or "", env["AVA_REDIS_PASSWORD"] or ""
    assert admin and runtime and admin != runtime
    url = urlsplit(env["AVA_REDIS_URL"] or "")
    assert (url.username, url.password, url.port) == ("ava", runtime, port)
    assert cutover.read_journal(home) == {"redis": "done"}
    assert cutover.journal_path(home).stat().st_mode & 0o777 == 0o600
    # No retry: redis-py would otherwise back off and repeat the refused AUTH.
    anonymous = redis.Redis(port=port, retry=Retry(NoBackoff(), 0))
    with anonymous, pytest.raises(redis.AuthenticationError):
        anonymous.ping()  # pyright: ignore[reportUnknownMemberType] — redis command stubs
    with redis.Redis(port=port, username="ava", password=runtime) as runtime_user:
        assert runtime_user.get("continuation") == b"kept"  # pyright: ignore[reportUnknownMemberType] — redis command stubs
    with redis.Redis(port=port, password=admin, decode_responses=True) as administrator:
        user = cast("dict[str, list[str]]", administrator.acl_getuser("ava"))  # pyright: ignore[reportUnknownMemberType] — redis command stubs
    assert "nopass" not in user["flags"]
    assert 'requirepass "' in (home / "redis" / "redis.conf").read_text()
    return admin


def test_dry_run_reports_the_conversion_and_changes_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _legacy_home(home, monkeypatch)
    before = (home / ".env").read_bytes()
    assert cutover.convert_redis(home, port, execute=False).startswith("redis: would mint")
    assert (home / ".env").read_bytes() == before
    assert not cutover.journal_path(home).exists()
    assert cutover.live_posture(port) == "unauthenticated"


def test_converts_an_unauthenticated_home_keeping_its_data(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _legacy_home(home, monkeypatch)
    assert "mint complete" in cutover.convert_redis(home, port, execute=True)
    _assert_converted(home, port)


def test_repeat_is_a_verified_no_op(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = _legacy_home(home, monkeypatch)
    cutover.convert_redis(home, port, execute=True)
    admin = _assert_converted(home, port)
    converted = ((home / ".env").read_bytes(), cutover.journal_path(home).read_bytes())
    pid = _pid(port, admin)
    assert "verify complete" in cutover.convert_redis(home, port, execute=True)
    assert ((home / ".env").read_bytes(), cutover.journal_path(home).read_bytes()) == converted
    assert _pid(port, admin) == pid, "a converted home is verified, never restarted"


def test_resumes_after_credentials_landed_without_regenerating_them(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after the `.env` write but before the restart continues with the
    journaled step's written credentials; nothing is minted twice."""
    port = _free_port()
    _start_legacy_redis(home, port, monkeypatch)
    written = {**_OPEN_ENV, "AVA_REDIS_ADMIN_PASSWORD": "landed-admin"}
    written["AVA_REDIS_PASSWORD"] = _LANDED_RUNTIME
    _write_env(home, port, written, f"redis://ava:{_LANDED_RUNTIME}@127.0.0.1:{port}/0")
    cutover._record(home, "redis", "converting")
    before = (home / ".env").read_bytes()

    assert "adopt complete" in cutover.convert_redis(home, port, execute=True)
    assert (home / ".env").read_bytes() == before
    assert cutover.live_posture(port) == "authenticated"
    with redis.Redis(port=port, username="ava", password=_LANDED_RUNTIME) as runtime_user:
        assert runtime_user.get("continuation") == b"kept"  # pyright: ignore[reportUnknownMemberType] — redis command stubs


@pytest.mark.parametrize(
    ("values", "url_password", "match"),
    [
        ({"AVA_REDIS_ADMIN_PASSWORD": "admin"}, "", "ambiguous Redis credentials"),
        ({"AVA_REDIS_PASSWORD": "runtime"}, "runtime", "ambiguous Redis credentials"),
        (
            {"AVA_REDIS_ADMIN_PASSWORD": "admin", "AVA_REDIS_PASSWORD": "runtime"},
            "other",
            "ambiguous Redis credentials",
        ),
    ],
)
def test_partial_credentials_are_refused_before_any_effect(
    home: Path, values: dict[str, str], url_password: str, match: str
) -> None:
    port = _free_port()
    userinfo = f"ava:{url_password}" if url_password else "ava"
    _write_env(home, port, {**_OPEN_ENV, **values}, f"redis://{userinfo}@127.0.0.1:{port}/0")
    before = (home / ".env").read_bytes()
    with pytest.raises(cutover.CutoverRefusedError, match=match):
        cutover.convert_redis(home, port, execute=True)
    assert (home / ".env").read_bytes() == before
    assert not cutover.journal_path(home).exists()


def test_unrecorded_redis_password_and_unclaimed_credentials_are_refused(home: Path) -> None:
    with redis_server() as url:
        port = urlsplit(url).port
        assert port is not None
        # Credentials the home records, but a password-less Redis and no journal.
        credentialed = {"AVA_REDIS_ADMIN_PASSWORD": "admin", "AVA_REDIS_PASSWORD": "runtime"}
        _write_env(home, port, credentialed, f"redis://ava:runtime@127.0.0.1:{port}/0")
        with pytest.raises(cutover.CutoverRefusedError, match="no cutover journal claims"):
            cutover.convert_redis(home, port, execute=True)
        # No credentials recorded, but Redis demands a password.
        _write_env(home, port, _OPEN_ENV)
        with redis.Redis.from_url(url) as client:  # pyright: ignore[reportUnknownMemberType] — redis client stubs
            client.config_set("requirepass", _FOREIGN_ADMIN)  # pyright: ignore[reportUnknownMemberType] — redis command stubs
        with pytest.raises(cutover.CutoverRefusedError, match="does not record"):
            cutover.convert_redis(home, port, execute=True)
        assert not cutover.journal_path(home).exists()
        with redis.Redis(port=port, password=_FOREIGN_ADMIN) as client:
            client.config_set("requirepass", "")  # pyright: ignore[reportUnknownMemberType] — redis command stubs


@pytest.mark.parametrize(
    "journal",
    [
        '{"version": 1, "home": "HOME", "steps": {"redis": "done"}}\n',
        '{"version": 1, "home": "HOME", "steps": {"redis": "rotated"}}\n',
        '{"version": 1, "home": "/elsewhere", "steps": {}}\n',
        "not json",
    ],
)
def test_journal_that_contradicts_the_home_is_refused(home: Path, journal: str) -> None:
    port = _free_port()
    _write_env(home, port, _OPEN_ENV)
    cutover.journal_path(home).parent.mkdir(mode=0o700)
    cutover.journal_path(home).write_text(journal.replace("HOME", str(home)))
    with pytest.raises((cutover.CutoverRefusedError, ValueError)):
        cutover.convert_redis(home, port, execute=True)
    assert dotenv_values(home / ".env")["AVA_REDIS_ADMIN_PASSWORD"] == ""


def test_main_refuses_a_home_this_checkout_does_not_own(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    assert cutover.main(["--home", str(other), "--execute"]) == 1
    assert "is not this checkout's home" in capsys.readouterr().err
    assert not cutover.journal_path(other).exists()
