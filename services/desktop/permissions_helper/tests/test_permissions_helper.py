"""Permissions helper: client wire-protocol contract + capability/lifecycle helpers.

The Swift helper, code-signing, launchd, and TCC are macOS-host concerns and are
not exercised by this portable test module; a dedicated macOS CI lane covers the
real native chain. The native server additionally enforces owner-only socket mode
and same-uid peers. What IS portable -- and what these tests pin -- is the
JSON-line contract the client speaks, verified against a pure-Python fake server,
plus the platform gate and the per-cluster naming.
"""

from __future__ import annotations

import json
import os
import plistlib
import socket
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from services.desktop.permissions_helper import client
from services.desktop.permissions_helper.client import PermissionsHelperError


def _read_line(conn: socket.socket) -> bytes:
    data = b""
    while not data.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    return data


class _FakeHelper:
    """A pure-Python Unix-socket server speaking the helper's JSON-line protocol.

    `handler` maps a request dict to a response dict (the common path). `raw`
    takes full control of the accepted connection (read + write) so tests can
    exercise partial writes, silent closes, and stalls."""

    def __init__(
        self,
        path: str,
        handler: Callable[[dict], dict] | None = None,
        raw: Callable[[socket.socket], None] | None = None,
    ) -> None:
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(path)
        self._srv.listen(8)
        self._srv.settimeout(0.25)
        self._handler = handler  # pyright: ignore[reportUnknownMemberType]
        self._raw = raw
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except TimeoutError:
                continue
            with conn:
                if self._raw is not None:
                    self._raw(conn)
                    continue
                assert self._handler is not None  # pyright: ignore[reportUnknownMemberType]
                resp = self._handler(json.loads(_read_line(conn)))  # pyright: ignore[reportUnknownMemberType]
                conn.sendall((json.dumps(resp) + "\n").encode())

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._srv.close()


@pytest.fixture
def fake_helper():
    servers: list[_FakeHelper] = []
    paths: list[str] = []

    def start(
        handler: Callable[[dict], dict] | None = None,
        *,
        raw: Callable[[socket.socket], None] | None = None,
    ) -> str:
        # Keep the path short: AF_UNIX sun_path caps near 104 chars, well under
        # what pytest's tmp_path would produce.
        path = f"/tmp/avah{os.getpid()}-{len(servers)}.sock"  # noqa: S108
        Path(path).unlink(missing_ok=True)
        servers.append(_FakeHelper(path, handler, raw))
        paths.append(path)
        return path

    yield start
    for s in servers:
        s.close()
    for p in paths:
        Path(p).unlink(missing_ok=True)


def test_call_echoes_method_and_returns_result(fake_helper) -> None:
    seen: dict = {}

    def handler(req: dict) -> dict:
        seen.update(req)  # pyright: ignore[reportUnknownMemberType]
        return {"id": req["id"], "ok": True, "result": {"path": req["path"], "bytes": 42}}

    path = fake_helper(handler)
    out = client.screencapture_region(1, 2, 3, 4, "out.png", sock_path=path)  # pyright: ignore[reportUnknownArgumentType]
    assert out == {"path": "out.png", "bytes": 42}
    assert seen["method"] == "screencapture_region"
    assert (seen["x"], seen["y"], seen["w"], seen["h"]) == (1, 2, 3, 4)


def test_ok_false_raises_with_server_error(fake_helper) -> None:
    path = fake_helper(lambda req: {"id": req["id"], "ok": False, "error": "no focused window"})
    with pytest.raises(PermissionsHelperError, match="no focused window"):
        client.ax_window_info("Finder", sock_path=path)  # pyright: ignore[reportUnknownArgumentType]


def test_unreachable_socket_raises() -> None:
    with pytest.raises(PermissionsHelperError, match="not reachable"):
        client.ping(sock_path="/tmp/ava-native-does-not-exist.sock")  # noqa: S108 — nonexistent path, asserts the unreachable error


def test_socket_path_keyed_on_port(monkeypatch: pytest.MonkeyPatch) -> None:
    from base import paths

    monkeypatch.setattr(paths.settings.services, "permissions_helper_port", 9999)
    assert paths.permissions_helper_socket().name == "permissions-helper.9999.sock"


def test_permissions_helper_app_dir_is_stable_under_ava_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from base import paths

    monkeypatch.setenv("AVA_HOME", str(tmp_path / "home"))
    assert paths.permissions_helper_app_dir() == tmp_path / "home" / "helper"


def test_method_name_mapping(fake_helper) -> None:
    # type_text dispatches as method "type", not "type_text" -- pin that wire name.
    seen: list[str] = []

    def handler(req: dict) -> dict:
        seen.append(req["method"])  # pyright: ignore[reportUnknownArgumentType]
        return {"id": req["id"], "ok": True, "result": {}}

    path = fake_helper(handler)
    client.type_text("hi", sock_path=path)  # pyright: ignore[reportUnknownArgumentType]
    client.click(1, 2, sock_path=path)  # pyright: ignore[reportUnknownArgumentType]
    client.ping(sock_path=path)  # pyright: ignore[reportUnknownArgumentType]
    assert seen == ["type", "click", "ping"]


def test_nursery_method_requests_and_results(fake_helper) -> None:
    seen: list[dict] = []

    def handler(req: dict) -> dict:
        seen.append({key: value for key, value in req.items() if key != "id"})  # pyright: ignore[reportUnknownMemberType]
        results: dict[str, object] = {
            "spawn": {"pid": 4123, "reused": False},
            "session_list": {"sessions": [{"name": "agent-demo", "pid": 4123, "alive": True}]},
            "session_has": {"alive": True},
            "signal": {"sent": True},
        }
        return {"id": req["id"], "ok": True, "result": results[req["method"]]}

    path = fake_helper(handler)
    assert client.spawn_process(
        "agent-demo",
        ["/usr/bin/env", "python"],
        {"AVA_HOME": "/Users/ava/.ava"},
        "/Users/ava/work",
        "/Users/ava/logs/stdout.log",
        "/Users/ava/logs/stderr.log",
        sock_path=path,  # pyright: ignore[reportUnknownArgumentType]
    ) == {"pid": 4123, "reused": False}
    assert client.session_list("agent-", sock_path=path) == [  # pyright: ignore[reportUnknownArgumentType]
        {"name": "agent-demo", "pid": 4123, "alive": True}
    ]
    assert client.session_has("agent-demo", sock_path=path)  # pyright: ignore[reportUnknownArgumentType]
    assert client.signal_session(name="agent-demo", sig=2, sock_path=path)  # pyright: ignore[reportUnknownArgumentType]
    assert client.signal_session(pid=4123, sock_path=path)  # pyright: ignore[reportUnknownArgumentType]

    assert seen == [
        {
            "method": "spawn",
            "name": "agent-demo",
            "argv": ["/usr/bin/env", "python"],
            "env": {"AVA_HOME": "/Users/ava/.ava"},
            "cwd": "/Users/ava/work",
            "stdout": "/Users/ava/logs/stdout.log",
            "stderr": "/Users/ava/logs/stderr.log",
        },
        {"method": "session_list", "prefix": "agent-"},
        {"method": "session_has", "name": "agent-demo"},
        {"method": "signal", "sig": 2, "name": "agent-demo"},
        {"method": "signal", "sig": 15, "pid": 4123},
    ]


def test_spawn_process_preserves_reused_result(fake_helper) -> None:
    def handler(req: dict) -> dict:
        return {"id": req["id"], "ok": True, "result": {"pid": 4123, "reused": True}}

    assert client.spawn_process(
        "agent-demo",
        ["/usr/bin/env", "python"],
        {},
        "/Users/ava/work",
        "/Users/ava/logs/stdout.log",
        "/Users/ava/logs/stderr.log",
        sock_path=fake_helper(handler),  # pyright: ignore[reportUnknownArgumentType]
    ) == {"pid": 4123, "reused": True}


def test_native_spawn_contract_requires_absolute_output_paths_without_redundant_dup2() -> None:
    source = (
        Path(__file__).parents[4] / "services/desktop/permissions_helper/helper/main.swift"
    ).read_text()
    spawn_source = source.split("func spawnProcess", 1)[1].split("func sessionList", 1)[0]

    assert "(stdoutPath as NSString).isAbsolutePath" in spawn_source
    assert "(stderrPath as NSString).isAbsolutePath" in spawn_source
    assert (
        "posix_spawn_file_actions_adddup2(&fileActions, STDIN_FILENO, STDIN_FILENO)"
        not in spawn_source
    )


def test_spawn_core_resets_child_signal_state() -> None:
    source = (
        Path(__file__).parents[4] / "services/desktop/permissions_helper/helper/main.swift"
    ).read_text()
    spawn_source = source.split("func spawnDetachedChild", 1)[1].split("func sessionList", 1)[0]

    # Children inherit the calling thread's signal mask through posix_spawn, and
    # the root keeper spawns from a GCD worker thread where libdispatch blocks
    # most signals including SIGTERM. Without the reset below, a keeper-spawned
    # ava-root never receives SIGTERM (confirmed empirically 2026-09-12).
    assert "POSIX_SPAWN_SETSIGMASK" in spawn_source
    assert "posix_spawnattr_setsigmask(&attributes, &emptySignalMask)" in spawn_source
    assert "POSIX_SPAWN_SETSIGDEF" in spawn_source
    assert "posix_spawnattr_setsigdefault(&attributes, &signalsToDefault)" in spawn_source


def _assert_spawn_core_and_lock_file_pinned(keeper_source: str) -> None:
    # The CTO-ruled session-boundary sentence must live where root is spawned.
    assert "session boundary only" in keeper_source
    assert "ppid unchanged, no reparent" in keeper_source
    # Single instance rides on the root's own lock file, probed non-blockingly.
    assert '(runDir as NSString).appendingPathComponent("ava-root.lock")' in keeper_source
    assert "flock(fd, LOCK_EX | LOCK_NB)" in keeper_source
    # Root goes through the shared detached-child spawn core, never the
    # session table.
    assert "spawnDetachedChild(" in keeper_source
    assert "children[" not in keeper_source


def _assert_stop_seed_and_wiring_pinned(keeper_source: str, source: str) -> None:
    # A stop the keeper requested must never restart root.
    assert 'let requested = stopRequested || state == "stopping"' in keeper_source
    # The SIGCHLD drain uses one ownership boundary for both child owners.
    assert "rootKeeper.reapExitedChildLocked()" in source
    # Startup seeding is opt-in via the seed environment; absent = dormant.
    assert 'rootSeedEnvironment = "AVA_PERMISSIONS_HELPER_ROOT_SEED"' in source
    assert "rootKeeper.startIfConfigured()" in source
    # The wire surface is wired into dispatch.
    for method in ("root_seed", "root_status", "root_stop"):
        assert f'case "{method}"' in source


def _assert_spawn_and_reap_share_one_lock_domain(keeper_source: str) -> None:
    # Spawn and ownership accounting share one lock domain with the SIGCHLD
    # reap: no unlock may sit between the spawn call and the pid record, or a
    # root that exits inside the spawn window drains unattributed and parks
    # the keeper on a dead pid (QA #3242).
    attempt = keeper_source.split("private func attemptSpawn", 1)[1].split(
        "private func scheduleSpawnRetry", 1
    )[0]
    spawn_at = attempt.index("spawnDetachedChild(")
    account_at = attempt.index("childPID = pid")
    lock_at = attempt.rindex("lock.lock()", 0, spawn_at)
    assert "lock.unlock()" not in attempt[spawn_at:account_at]
    assert lock_at < spawn_at
    assert "StopIntent.exists(seed.runDir, owner: .root)" in attempt[lock_at:spawn_at]


def test_root_keeper_contract_is_pinned_in_the_swift_source() -> None:
    source = (
        Path(__file__).parents[4] / "services/desktop/permissions_helper/helper/main.swift"
    ).read_text()
    keeper_source = source.split("// MARK: - Root keeper", 1)[1].split("// MARK: - Dispatch", 1)[0]

    _assert_spawn_core_and_lock_file_pinned(keeper_source)
    _assert_stop_seed_and_wiring_pinned(keeper_source, source)
    _assert_spawn_and_reap_share_one_lock_domain(keeper_source)


def test_root_seed_env_rejects_a_non_string_map() -> None:
    source = (
        Path(__file__).parents[4] / "services/desktop/permissions_helper/helper/main.swift"
    ).read_text()
    seed_source = source.split("struct RootSeed", 1)[1].split("static func load", 1)[0]

    # A present-but-mistyped `env` fails fast like every other seed field,
    # instead of the cast collapsing to an empty map and silently dropping
    # the child's environment (QA #3242).
    assert 'raw["env"] as? [String: String]) ?? [:]' not in seed_source
    assert "root seed: env must be a map of string to string" in seed_source
    assert "environment = [:]" in seed_source


@pytest.mark.parametrize(
    ("name", "pid"),
    [(None, None), ("agent-demo", 4123)],
)
def test_signal_session_requires_exactly_one_target(name: str | None, pid: int | None) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        client.signal_session(name=name, pid=pid)


# SECURITY SYNC: mirrors the resolved-string boundary check in
# services/desktop/permissions_helper/helper/main.swift::resolvedWhitelistedFilePath.
# Update both implementations together whenever whitelist containment changes.
def _is_whitelisted_file_path(path: Path, roots: list[Path]) -> bool:
    if not path.is_absolute():
        return False
    resolved_path = str(path.resolve())
    return any(
        resolved_path == (resolved_root := str(root.resolve()))
        or resolved_path.startswith(resolved_root + "/")
        for root in roots
    )


# --- Screen-capture probe -------------------------------------------------
# The grant that decides OS-level capture belongs to the helper, so the probe
# reads it from the helper; the calling process's own grant is a different fact.


def _ping_reply(preflight_screen: bool, ax_trusted: bool = True) -> Callable[[dict], dict]:
    return lambda req: {
        "id": req["id"],
        "ok": True,
        "result": {
            "pong": True,
            "preflight_screen": preflight_screen,
            "ax_trusted": ax_trusted,
        },
    }


# --- Accessibility probe --------------------------------------------------
# Like screen capture, this asks the helper because its grant -- not the
# caller's inherited grant -- determines whether macOS accepts the action.


# --- Bundle content -------------------------------------------------------


def _parse_strings(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("/*", "//")):
            continue
        key_part, separator, value_part = line.partition(" = ")
        assert separator, line
        entries[key_part.strip('"')] = value_part.rstrip(";").strip('"')
    return entries


# --- Signing --------------------------------------------------------------
# TCC keys the helper's grants on the stable certificate, so an ad-hoc identity
# is never an acceptable substitute -- and a current bundle is never re-signed.


_TEST_CERT_SHA1 = "0123456789ABCDEF0123456789ABCDEF01234567"


def _stage_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, exe_present: bool) -> Path:
    from services.desktop.permissions_helper import launchd_job, lifecycle

    src = tmp_path / "main.swift"
    src.write_text("// swift")
    info = tmp_path / "Info.plist"
    info.write_bytes(b"<plist/>")
    entitlements = tmp_path / "helper.entitlements"
    entitlements.write_bytes(b"<plist/>")
    build = tmp_path / "installed-helper"
    app = build / "AvaPermissionsHelper.app"
    if exe_present:
        exe = app / "Contents" / "MacOS" / "AvaPermissionsHelper"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"\x00")  # written last, so its mtime is at least the sources'
        monkeypatch.setattr(launchd_job, "helper_job_loaded", lambda: True)  # its home runs it
    locales = tmp_path / "locales"
    en_lproj = locales / "en.lproj"
    en_lproj.mkdir(parents=True)
    (en_lproj / "Localizable.strings").write_text('"panel.title" = "Ava Permissions Helper";')
    zh_lproj = locales / "zh-Hans.lproj"
    zh_lproj.mkdir()
    (zh_lproj / "Localizable.strings").write_text('"panel.title" = "zh";')
    monkeypatch.setattr(lifecycle, "_SOURCE", src)
    monkeypatch.setattr(lifecycle, "_INFO_PLIST", info)
    monkeypatch.setattr(lifecycle, "_ENTITLEMENTS", entitlements)
    monkeypatch.setattr(lifecycle, "_LOCALES", locales)
    monkeypatch.setattr("base.paths.permissions_helper_app_dir", lambda: build)
    return app


def _test_dr() -> str:
    return (
        'identifier "com.ava.permissions-helper" and certificate leaf = '
        f'H"{_TEST_CERT_SHA1.lower()}"'
    )


def _write_current_build_state(app: Path, source_hash: str, *, dr: str | None = None) -> None:
    (app.parent / "build-state.json").write_text(
        json.dumps(
            {
                "source_hash": source_hash,
                "dr": dr or _test_dr(),
                "signed_at": "2026-09-05T00:00:00+00:00",
            }
        )
    )


class _Call(NamedTuple):
    """One `run_bounded` invocation: what was run, and under what bound."""

    argv: list[str]
    timeout: float


@dataclass(frozen=True)
class _FakeToolOutputs:
    """What the stand-in swiftc / codesign / security answer, per `_fake_tools`'s arguments."""

    authority: str | None
    keychain_rc: int
    sign_rc: int
    acl_probe_rc: int
    smoke_sign_rc: int
    smoke_sign_stderr: bytes
    verify_rc: int
    designated_requirement: str | None
    dr_streams: tuple[bytes, bytes] | None
    identity_output: str | None
    list_keychains_output: bytes | None
    list_keychains_rc: int

    def respond(self, cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
        """The result of one tool invocation; unmatched commands succeed silently."""
        if cmd[0] == "swiftc":
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"\x00")
        if cmd[:3] == ["security", "find-identity", "-p"]:
            return self._find_identity(cmd)
        if cmd[:4] == ["security", "list-keychains", "-d", "user"]:
            return self._list_keychains(cmd)
        if cmd[:2] == ["codesign", "--verify"]:
            return subprocess.CompletedProcess(cmd, self.verify_rc, b"", b"invalid")
        if cmd[:2] == ["codesign", "--display"]:
            shown = f"Authority={self.authority}\n" if self.authority else "Signature=adhoc\n"
            return subprocess.CompletedProcess(cmd, 0, b"", shown.encode())
        if cmd[:3] == ["codesign", "-d", "-r-"]:
            return self._designated_requirement(cmd)
        if cmd[:2] == ["security", "show-keychain-info"] and self.keychain_rc != 0:
            return subprocess.CompletedProcess(
                cmd, self.keychain_rc, b"", b"User interaction is not allowed."
            )
        if cmd[:2] == ["codesign", "--sign"]:
            return self._sign_probe(cmd)
        if cmd[:2] == ["codesign", "--force"] and self.sign_rc != 0:
            return subprocess.CompletedProcess(cmd, self.sign_rc, b"", b"errSecInternalComponent")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    def _find_identity(self, cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
        from services.desktop.permissions_helper import lifecycle

        output = self.identity_output or f'  1) {_TEST_CERT_SHA1} "{lifecycle._CERT_CN}"\n'
        return subprocess.CompletedProcess(cmd, 0, output.encode(), b"")

    def _list_keychains(self, cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
        from services.desktop.permissions_helper import lifecycle

        output = self.list_keychains_output
        if output is None:
            output = f'    "{lifecycle._keychain_path()}"\n'.encode()
        return subprocess.CompletedProcess(
            cmd,
            self.list_keychains_rc,
            output,
            b"search list unavailable" if self.list_keychains_rc else b"",
        )

    def _designated_requirement(self, cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
        if self.dr_streams is not None:
            return subprocess.CompletedProcess(cmd, 0, *self.dr_streams)
        dr = self.designated_requirement or _test_dr()
        # Real codesign on current macOS emits the DR line on stdout
        # (stderr carries `Executable=...`); the reader accepts either.
        return subprocess.CompletedProcess(cmd, 0, f"designated => {dr}\n".encode(), b"")

    def _sign_probe(self, cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
        smoke = Path(cmd[-1]).name == "signing-smoke"
        rc = self.smoke_sign_rc if smoke else self.acl_probe_rc
        stderr = self.smoke_sign_stderr if smoke else b"errSecInternalComponent"
        return subprocess.CompletedProcess(cmd, rc, b"", stderr)


def _fake_tools(
    monkeypatch: pytest.MonkeyPatch,
    *,
    authority: str | None,
    keychain_rc: int = 0,
    sign_rc: int = 0,
    acl_probe_rc: int = 0,
    smoke_sign_rc: int = 0,
    smoke_sign_stderr: bytes = b"errSecInternalComponent",
    verify_rc: int = 0,
    designated_requirement: str | None = None,
    dr_streams: tuple[bytes, bytes] | None = None,
    identity_output: str | None = None,
    list_keychains_output: bytes | None = None,
    list_keychains_rc: int = 0,
    hang: tuple[str, ...] = (),
) -> list[_Call]:
    """Stand in for swiftc / codesign / security, recording every invocation.

    `authority` is what `codesign --display` reports for the staged bundle: the
    certificate CN, or None for an ad-hoc signature (which has no Authority).
    `acl_probe_rc` is what the pre-sign ACL probe (`codesign --sign` on a scratch
    file) exits with. `hang` is an argv prefix whose call raises TimeoutExpired
    exactly as `run_bounded` does once it has killed the tree -- the real hang is
    a GUI dialog no test may summon.

    This patches `run_bounded`, not `subprocess.run`: routing every call through
    it IS the invariant under test, so a call site that regressed to plain
    `subprocess.run` would reach the real tool and fail here rather than pass
    against a stub."""
    from services.desktop.permissions_helper import lifecycle

    outputs = _FakeToolOutputs(
        authority=authority,
        keychain_rc=keychain_rc,
        sign_rc=sign_rc,
        acl_probe_rc=acl_probe_rc,
        smoke_sign_rc=smoke_sign_rc,
        smoke_sign_stderr=smoke_sign_stderr,
        verify_rc=verify_rc,
        designated_requirement=designated_requirement,
        dr_streams=dr_streams,
        identity_output=identity_output,
        list_keychains_output=list_keychains_output,
        list_keychains_rc=list_keychains_rc,
    )
    recorded: list[_Call] = []

    def run(cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        argv = list(cmd)
        # Every call must arrive with a bound -- KeyError here means an unbounded
        # call site slipped back in.
        recorded.append(_Call(argv, kwargs["timeout"]))
        if hang and argv[: len(hang)] == list(hang):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return outputs.respond(argv)

    monkeypatch.setattr(lifecycle, "run_bounded", run)
    return recorded


def _argvs(recorded: list[_Call]) -> list[list[str]]:
    return [c.argv for c in recorded]


def _sign_command(recorded: list[_Call]) -> list[str]:
    return next(c.argv for c in recorded if c.argv[:2] == ["codesign", "--force"])


# --- Bounded calls + pre-sign probes --------------------------------------
# Nothing in lifecycle.py touches a network, so a step that runs long is one
# waiting on a human. On 2026-08-02 a headless rollout sat 67 minutes inside
# `codesign --sign` on a SecurityAgent dialog nobody could answer -- the abort
# path was fine, the trigger that converts a hang into a failure was missing.
# These pin that trigger: every call carries a bound, an expired bound raises,
# and the prompt is probed for before anything is compiled or written.


# --- Install, in-place upgrade, and launchd repair ------------------------


def _install_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    loaded: bool,
    matching_plist: bool = True,
) -> tuple[Path, list[list[str]], list[list[str]]]:
    import subprocess

    from services.desktop.permissions_helper import lifecycle

    app = tmp_path / "helper" / "AvaPermissionsHelper.app"
    exe = app / "Contents" / "MacOS" / "AvaPermissionsHelper"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"signed helper")
    agents = tmp_path / "LaunchAgents"
    plist_path = agents / "com.ava.permissions-helper.test.plist"
    log = tmp_path / "logs" / "permissions-helper.log"
    socket_path = tmp_path / "run" / "permissions-helper.sock"
    monkeypatch.setattr(lifecycle.base.paths, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(lifecycle, "_refuse_stale_jobs", lambda: None)
    monkeypatch.setattr(lifecycle, "_label", lambda: "com.ava.permissions-helper.test")
    monkeypatch.setattr(lifecycle, "_domain", lambda: "gui/501")
    monkeypatch.setattr(lifecycle, "_plist_path", lambda: plist_path)
    monkeypatch.setattr(lifecycle, "_is_loaded", lambda: loaded)
    monkeypatch.setattr(lifecycle, "logs_dir", lambda: log.parent)
    monkeypatch.setattr(lifecycle, "permissions_helper_socket", lambda: socket_path)
    run_calls: list[list[str]] = []
    probe_calls: list[list[str]] = []

    def run(cmd: list[str]) -> None:
        run_calls.append(cmd)

    def probe(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        probe_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(lifecycle, "_run", run)
    monkeypatch.setattr(lifecycle, "_probe", probe)
    if matching_plist:
        plist_path.parent.mkdir(parents=True)
        plist_path.write_bytes(
            plistlib.dumps(
                {
                    "Label": "com.ava.permissions-helper.test",
                    "ProgramArguments": [str(exe)],
                    "EnvironmentVariables": {
                        "AVA_PERMISSIONS_HELPER_SOCKET": str(socket_path),
                        "AVA_PERMISSIONS_HELPER_ROOT_SEED": str(
                            lifecycle.base.paths.root_run_dir() / "seed.json"
                        ),
                    },
                    "RunAtLoad": True,
                    "KeepAlive": {"SuccessfulExit": False},
                    "StandardOutPath": str(log),
                    "StandardErrorPath": str(log),
                }
            )
        )
    return app, run_calls, probe_calls


def _skip_sleep(_seconds: float) -> None:
    return


# --- Old-layout job retirement -------------------------------------------
# Labels used to be the fixed `com.ava.permissions-helper.main`; after
# per-cluster home-slug labels arrived, converge wrote the slugged job but never
# retired a `main` job already loaded, so two KeepAlive jobs raced the same
# socket.


def _write_agent_plist(
    agents: Path, label: str, sock: str, env_key: str = "AVA_PERMISSIONS_HELPER_SOCKET"
) -> Path:
    p = agents / f"{label}.plist"
    p.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": ["/usr/bin/true"],
                "EnvironmentVariables": {env_key: sock},
            }
        )
    )
    return p


def _retire_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, port: int = 9223) -> Path:
    """Point the retire machinery at a fake home: current socket, agents dir,
    launchd domain."""
    from services.desktop.permissions_helper import lifecycle

    agents = tmp_path / "LaunchAgents"
    agents.mkdir(parents=True)
    run = tmp_path / "run"
    run.mkdir(parents=True)
    monkeypatch.setattr(lifecycle, "_agents_dir", lambda: agents)
    monkeypatch.setattr(lifecycle, "_domain", lambda: "gui/501")
    monkeypatch.setattr(
        lifecycle, "permissions_helper_socket", lambda: run / f"permissions-helper.{port}.sock"
    )
    return agents
