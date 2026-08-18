"""Permissions helper: client wire-protocol contract + capability/lifecycle helpers.

The Swift helper, code-signing, launchd, and TCC are macOS-host concerns and are
not exercised here (CI is headless Linux). What IS portable -- and what these
tests pin -- is the JSON-line contract the client speaks, verified against a
pure-Python fake server, plus the platform gate and the per-cluster naming.
"""

from __future__ import annotations

import json
import os
import plistlib
import socket
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pytest

from services.permissions_helper import client
from services.permissions_helper.client import PermissionsHelperError
from shared.screen_capture import ScreenCaptureState


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
        handler: Callable[[dict], dict] | None = None,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
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
                resp = self._handler(json.loads(_read_line(conn)))  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                conn.sendall((json.dumps(resp) + "\n").encode())

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._srv.close()


@pytest.fixture
def fake_helper():  # pyright: ignore[reportUnknownParameterType]
    servers: list[_FakeHelper] = []
    paths: list[str] = []

    def start(
        handler: Callable[[dict], dict] | None = None,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
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


def test_call_echoes_method_and_returns_result(fake_helper) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    seen: dict = {}  # pyright: ignore[reportMissingTypeArgument, reportUnknownVariableType]

    def handler(req: dict) -> dict:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
        seen.update(req)  # pyright: ignore[reportUnknownMemberType]
        return {"id": req["id"], "ok": True, "result": {"path": req["path"], "bytes": 42}}  # pyright: ignore[reportUnknownVariableType]

    path = fake_helper(handler)  # pyright: ignore[reportUnknownVariableType]
    out = client.screencapture_region(1, 2, 3, 4, "out.png", sock_path=path)  # pyright: ignore[reportUnknownArgumentType]
    assert out == {"path": "out.png", "bytes": 42}
    assert seen["method"] == "screencapture_region"
    assert (seen["x"], seen["y"], seen["w"], seen["h"]) == (1, 2, 3, 4)


def test_ok_false_raises_with_server_error(fake_helper) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    path = fake_helper(lambda req: {"id": req["id"], "ok": False, "error": "no focused window"})  # pyright: ignore[reportUnknownLambdaType, reportUnknownVariableType]
    with pytest.raises(PermissionsHelperError, match="no focused window"):
        client.ax_window_info("Finder", sock_path=path)  # pyright: ignore[reportUnknownArgumentType]


def test_unreachable_socket_raises() -> None:
    with pytest.raises(PermissionsHelperError, match="not reachable"):
        client.ping(sock_path="/tmp/ava-native-does-not-exist.sock")  # noqa: S108 — nonexistent path, asserts the unreachable error


def test_socket_path_keyed_on_port(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared import paths

    monkeypatch.setattr(paths.settings.services, "permissions_helper_port", 9999)
    assert paths.permissions_helper_socket().name == "permissions-helper.9999.sock"


def test_method_name_mapping(fake_helper) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    # type_text dispatches as method "type", not "type_text" -- pin that wire name.
    seen: list[str] = []

    def handler(req: dict) -> dict:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
        seen.append(req["method"])  # pyright: ignore[reportUnknownArgumentType]
        return {"id": req["id"], "ok": True, "result": {}}  # pyright: ignore[reportUnknownVariableType]

    path = fake_helper(handler)  # pyright: ignore[reportUnknownVariableType]
    client.type_text("hi", sock_path=path)  # pyright: ignore[reportUnknownArgumentType]
    client.click(1, 2, sock_path=path)  # pyright: ignore[reportUnknownArgumentType]
    client.ping(sock_path=path)  # pyright: ignore[reportUnknownArgumentType]
    assert seen == ["type", "click", "ping"]


def test_gui_ops_wire_requests(fake_helper) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    # Pin the wire request each GUI-driver wrapper emits (method name + args),
    # since the WeChat skill depends on these exact shapes.
    seen: list[dict] = []  # pyright: ignore[reportMissingTypeArgument, reportUnknownVariableType]

    def handler(req: dict) -> dict:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
        seen.append({k: v for k, v in req.items() if k != "id"})  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        return {"id": req["id"], "ok": True, "result": {}}  # pyright: ignore[reportUnknownVariableType]

    path = fake_helper(handler)  # pyright: ignore[reportUnknownVariableType]
    client.key(76, sock_path=path)  # pyright: ignore[reportUnknownArgumentType]
    client.key(9, cmd=True, sock_path=path)  # pyright: ignore[reportUnknownArgumentType]
    client.scroll(10, 20, -50, sock_path=path)  # pyright: ignore[reportUnknownArgumentType]
    client.window_info("WeChat", sock_path=path)  # pyright: ignore[reportUnknownArgumentType]
    client.session_info(sock_path=path)  # pyright: ignore[reportUnknownArgumentType]
    client.click(5, 6, double=True, sock_path=path)  # pyright: ignore[reportUnknownArgumentType]
    assert seen == [
        {"method": "key", "code": 76, "cmd": False},
        {"method": "key", "code": 9, "cmd": True},
        {"method": "scroll", "x": 10, "y": 20, "dy": -50},
        {"method": "window_info", "owner": "WeChat"},
        {"method": "session_info"},
        {"method": "click", "x": 5, "y": 6, "double": True},
    ]


def test_incapability_branch_ordering(monkeypatch: pytest.MonkeyPatch) -> None:
    # Probe ordering only ever reaches the first branch on Linux CI; drive the
    # macOS branches explicitly so a reordered or dropped prong is caught.
    from shared import platform_probes as pp

    def reason() -> str:
        r = pp.permissions_helper_incapability()
        assert r is not None
        return r

    monkeypatch.setattr(sys, "platform", "darwin")
    present = {"swiftc": "/usr/bin/swiftc", "codesign": "/usr/bin/codesign"}
    monkeypatch.setattr(pp, "display_available", lambda: True)

    monkeypatch.setattr(pp.shutil, "which", present.get)
    assert pp.permissions_helper_incapability() is None

    monkeypatch.setattr(pp.shutil, "which", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    assert "no swiftc" in reason()

    monkeypatch.setattr(pp.shutil, "which", lambda n: "/usr/bin/swiftc" if n == "swiftc" else None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    assert "no codesign" in reason()

    monkeypatch.setattr(pp.shutil, "which", present.get)
    monkeypatch.setattr(pp, "display_available", lambda: False)
    assert "no display" in reason()

    monkeypatch.setattr(sys, "platform", "linux")
    assert "macOS or Windows only" in reason()


def test_incapability_windows_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows is capable when csc.exe (the .NET Framework compiler) is present,
    and names the fix when it is not."""
    from shared import platform_probes as pp

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        pp,
        "WINDOWS_CSC_CANDIDATES",
        (r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",),
    )

    monkeypatch.setattr(pp.Path, "exists", lambda _self: True)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    assert pp.permissions_helper_incapability() is None
    assert pp.permissions_helper_capable() is True

    monkeypatch.setattr(pp.Path, "exists", lambda _self: False)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    reason = pp.permissions_helper_incapability()
    assert reason is not None
    assert "no csc.exe" in reason


def test_recv_reassembles_across_chunks(fake_helper) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    def raw(conn: socket.socket) -> None:
        _read_line(conn)
        payload = json.dumps({"id": 1, "ok": True, "result": {"pong": True}}).encode() + b"\n"
        conn.sendall(payload[:5])
        time.sleep(0.05)
        conn.sendall(payload[5:])

    assert client.ping(sock_path=fake_helper(raw=raw)) == {"pong": True}  # pyright: ignore[reportUnknownArgumentType]


def test_closed_without_response_raises(fake_helper) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    with pytest.raises(PermissionsHelperError, match="closed without a response"):
        client.ping(
            sock_path=fake_helper(raw=_read_line)  # pyright: ignore[reportUnknownArgumentType]
        )  # read, then close, no reply  # pyright: ignore[reportUnknownArgumentType]


def test_truncated_response_raises(fake_helper) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    def raw(conn: socket.socket) -> None:
        _read_line(conn)
        conn.sendall(b'{"id":1,"ok":tr')  # partial JSON, no trailing newline

    with pytest.raises(PermissionsHelperError, match="truncated"):
        client.ping(sock_path=fake_helper(raw=raw))  # pyright: ignore[reportUnknownArgumentType]


def test_call_timeout_raises(fake_helper, monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    monkeypatch.setattr(client, "_CALL_TIMEOUT_S", 0.2)

    def raw(conn: socket.socket) -> None:
        _read_line(conn)
        time.sleep(1.0)  # accept, never reply

    with pytest.raises(PermissionsHelperError, match="did not respond"):
        client.ping(sock_path=fake_helper(raw=raw))  # pyright: ignore[reportUnknownArgumentType]


def test_line_limit_guard(fake_helper, monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    monkeypatch.setattr(client, "_LINE_LIMIT", 16)

    def raw(conn: socket.socket) -> None:
        _read_line(conn)
        conn.sendall(b"x" * 64)  # oversized, newline-less

    with pytest.raises(PermissionsHelperError, match="exceeded line limit"):
        client.ping(sock_path=fake_helper(raw=raw))  # pyright: ignore[reportUnknownArgumentType]


def test_connect_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    # Server binds only after the first connect attempt fails -> exercises the
    # retry loop that exists for the daemon-startup race.
    monkeypatch.setattr(client, "_CONNECT_DELAY_S", 0.1)
    path = f"/tmp/avah-retry{os.getpid()}.sock"  # noqa: S108 — short path for AF_UNIX
    Path(path).unlink(missing_ok=True)
    holder: list[_FakeHelper] = []

    def late_start() -> None:
        time.sleep(0.25)
        holder.append(
            _FakeHelper(path, lambda req: {"id": req["id"], "ok": True, "result": {"pong": True}})  # pyright: ignore[reportUnknownLambdaType]
        )

    t = threading.Thread(target=late_start, daemon=True)
    t.start()
    try:
        assert client.ping(sock_path=path) == {"pong": True}
    finally:
        t.join(timeout=2)
        for s in holder:
            s.close()
        Path(path).unlink(missing_ok=True)


def test_label_is_per_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    from services.permissions_helper import lifecycle
    from shared.cluster import home_slug

    monkeypatch.setattr("shared.paths.ava_home", lambda: Path("/x/.ava-demo"))
    assert lifecycle._label() == f"com.ava.permissions-helper.{home_slug(Path('/x/.ava-demo'))}"


# --- Screen-capture probe -------------------------------------------------
# The grant that decides OS-level capture belongs to the helper, so the probe
# reads it from the helper; the calling process's own grant is a different fact.


def _ping_reply(preflight_screen: bool) -> Callable[[dict], dict]:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    return lambda req: {  # pyright: ignore[reportUnknownLambdaType, reportUnknownVariableType]
        "id": req["id"],
        "ok": True,
        "result": {"pong": True, "preflight_screen": preflight_screen, "ax_trusted": True},
    }


def test_screen_capture_probe_reads_the_helpers_grant(fake_helper) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    seen: list[str] = []

    def handler(req: dict) -> dict:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
        seen.append(req["method"])  # pyright: ignore[reportUnknownArgumentType]
        return _ping_reply(True)(req)  # pyright: ignore[reportUnknownVariableType]

    status = client.check_screen_capture(sock_path=fake_helper(handler))  # pyright: ignore[reportUnknownArgumentType]
    assert seen == ["ping"]  # measured in the helper, not in whoever is asking
    assert status.state is ScreenCaptureState.AVAILABLE
    assert status.available is True


def test_screen_capture_probe_reports_no_grant(fake_helper) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    status = client.check_screen_capture(sock_path=fake_helper(_ping_reply(False)))  # pyright: ignore[reportUnknownArgumentType]
    assert status.state is ScreenCaptureState.NO_GRANT
    assert status.available is False
    # The fix is authorizing the helper, not restarting the cluster from a local
    # terminal -- the SSH/terminal story never applied to the helper's own grant.
    assert "AvaPermissionsHelper" in status.diagnostic
    assert "System Settings" in status.diagnostic
    assert "Terminal.app" not in status.diagnostic


def test_screen_capture_probe_keeps_unreachable_distinct_from_denied() -> None:
    status = client.check_screen_capture(
        sock_path="/tmp/ava-native-does-not-exist.sock",  # noqa: S108 — nonexistent by design
        settle_s=0.0,
    )
    assert status.state is ScreenCaptureState.HELPER_UNREACHABLE
    assert status.available is False
    # An unread grant is not a missing one: this fault is fixed at launchd, so
    # the message must not send the operator to the permissions pane.
    assert "launchctl" in status.diagnostic
    assert "System Settings" not in status.diagnostic


def test_screen_capture_probe_waits_out_a_cold_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    # launchd may have only just bootstrapped the helper, so one unreachable ping
    # inside the settle window is a cold start rather than a dead daemon.
    calls: list[int] = []

    def flaky(*, sock_path=None):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        calls.append(1)
        if len(calls) == 1:
            raise PermissionsHelperError("permissions helper not reachable")
        return {"pong": True, "preflight_screen": True, "ax_trusted": True}

    monkeypatch.setattr(client, "ping", flaky)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(client, "_PROBE_RETRY_DELAY_S", 0.01)
    status = client.check_screen_capture(settle_s=2.0)
    assert len(calls) == 2
    assert status.state is ScreenCaptureState.AVAILABLE


# --- Signing --------------------------------------------------------------
# TCC keys the helper's grants on the stable certificate, so an ad-hoc identity
# is never an acceptable substitute -- and a current bundle is never re-signed.


def _stage_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, exe_present: bool) -> Path:
    from services.permissions_helper import lifecycle

    src = tmp_path / "main.swift"
    src.write_text("// swift")
    info = tmp_path / "Info.plist"
    info.write_bytes(b"<plist/>")
    build = tmp_path / "build"
    app = build / "AvaPermissionsHelper.app"
    if exe_present:
        exe = app / "Contents" / "MacOS" / "AvaPermissionsHelper"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"\x00")  # written last, so its mtime is at least the sources'
    monkeypatch.setattr(lifecycle, "_SOURCE", src)
    monkeypatch.setattr(lifecycle, "_INFO_PLIST", info)
    monkeypatch.setattr(lifecycle, "_BUILD_DIR", build)
    return app


class _Call(NamedTuple):
    """One `run_bounded` invocation: what was run, and under what bound."""

    argv: list[str]
    timeout: float


def _fake_tools(
    monkeypatch: pytest.MonkeyPatch,
    *,
    authority: str | None,
    keychain_rc: int = 0,
    sign_rc: int = 0,
    acl_probe_rc: int = 0,
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
    import subprocess

    from services.permissions_helper import lifecycle

    recorded: list[_Call] = []

    def run(cmd, **kwargs):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        cmd = list(cmd)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
        # Every call must arrive with a bound -- KeyError here means an unbounded
        # call site slipped back in.
        recorded.append(_Call(cmd, kwargs["timeout"]))
        if hang and cmd[: len(hang)] == list(hang):
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])  # pyright: ignore[reportUnknownArgumentType]
        if cmd[0] == "swiftc":
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"\x00")  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        if cmd[:2] == ["codesign", "--display"]:
            shown = f"Authority={authority}\n" if authority else "Signature=adhoc\n"
            return subprocess.CompletedProcess(cmd, 0, b"", shown.encode())  # pyright: ignore[reportUnknownArgumentType]
        if cmd[:2] == ["security", "show-keychain-info"] and keychain_rc != 0:
            return subprocess.CompletedProcess(
                cmd,  # pyright: ignore[reportUnknownArgumentType]
                keychain_rc,
                b"",
                b"User interaction is not allowed.",  # pyright: ignore[reportUnknownArgumentType]
            )
        if cmd[:2] == ["codesign", "--sign"]:
            return subprocess.CompletedProcess(cmd, acl_probe_rc, b"", b"probe")  # pyright: ignore[reportUnknownArgumentType]
        if cmd[:2] == ["codesign", "--force"] and sign_rc != 0:
            return subprocess.CompletedProcess(cmd, sign_rc, b"", b"errSecInternalComponent")  # pyright: ignore[reportUnknownArgumentType]
        return subprocess.CompletedProcess(cmd, 0, b"", b"")  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(lifecycle, "run_bounded", run)  # pyright: ignore[reportUnknownArgumentType]
    return recorded


def _argvs(recorded: list[_Call]) -> list[list[str]]:
    return [c.argv for c in recorded]


def _sign_command(recorded: list[_Call]) -> list[str]:
    return next(c.argv for c in recorded if c.argv[:2] == ["codesign", "--force"])


def test_current_stable_signed_bundle_is_not_resigned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Re-signing a current binary changes its cdhash and drops the grants it
    already holds, so the skip-if-up-to-date gate has to survive intact."""
    from services.permissions_helper import lifecycle

    app = _stage_bundle(monkeypatch, tmp_path, exe_present=True)
    recorded = _fake_tools(monkeypatch, authority=lifecycle._CERT_CN)

    assert lifecycle.build_and_sign() == (app, False)
    assert not any(c[0] == "swiftc" for c in _argvs(recorded))
    assert not any(c[:2] == ["codesign", "--force"] for c in _argvs(recorded))


def test_locked_keychain_does_not_fail_a_current_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only a real rebuild needs the signing key, so an SSH host whose helper is
    already current converges without ever consulting the keychain -- or the key
    ACL, the other pre-sign check that reaches for it."""
    from services.permissions_helper import lifecycle

    app = _stage_bundle(monkeypatch, tmp_path, exe_present=True)
    recorded = _fake_tools(monkeypatch, authority=lifecycle._CERT_CN, keychain_rc=1)

    assert lifecycle.build_and_sign() == (app, False)
    assert not any(c[:2] == ["security", "show-keychain-info"] for c in _argvs(recorded))
    assert not any(c[:2] == ["codesign", "--sign"] for c in _argvs(recorded))


def test_fresh_build_signs_with_the_stable_certificate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.permissions_helper import lifecycle

    app = _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = _fake_tools(monkeypatch, authority=None)

    assert lifecycle.build_and_sign() == (app, True)
    assert _sign_command(recorded) == [
        "codesign",
        "--force",
        "--sign",
        lifecycle._CERT_CN,
        "--identifier",
        lifecycle._BUNDLE_ID,
        str(app),
    ]


def test_ad_hoc_signed_bundle_is_rebuilt_onto_the_stable_certificate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bundle an earlier build left ad-hoc carries a per-build identity whose
    grant TCC drops on every rebuild, so re-signing forfeits nothing live and is
    the only way that host returns to the identity the grants are keyed on."""
    from services.permissions_helper import lifecycle

    app = _stage_bundle(monkeypatch, tmp_path, exe_present=True)
    recorded = _fake_tools(monkeypatch, authority=None)

    assert lifecycle.build_and_sign() == (app, True)
    assert _sign_command(recorded)[3] == lifecycle._CERT_CN


def test_locked_keychain_refuses_instead_of_downgrading_to_ad_hoc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = _fake_tools(monkeypatch, authority=None, keychain_rc=1)

    with pytest.raises(lifecycle.PermissionsHelperBuildError) as err:
        lifecycle.build_and_sign()
    assert "User interaction is not allowed" in str(err.value)
    assert "unlock-keychain" in str(err.value)
    assert not any(c[0] == "swiftc" for c in _argvs(recorded))
    assert not any(c[:2] == ["codesign", "--force"] for c in _argvs(recorded))


def test_codesign_failure_names_the_ad_hoc_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    _fake_tools(monkeypatch, authority=None, sign_rc=1)

    with pytest.raises(lifecycle.PermissionsHelperBuildError) as err:
        lifecycle.build_and_sign()
    assert "errSecInternalComponent" in str(err.value)
    assert "unlock-keychain" in str(err.value)


# --- Bounded calls + the pre-sign ACL probe -------------------------------
# Nothing in lifecycle.py touches a network, so a step that runs long is one
# waiting on a human. On 2026-08-02 a headless rollout sat 67 minutes inside
# `codesign --sign` on a SecurityAgent dialog nobody could answer -- the abort
# path was fine, the trigger that converts a hang into a failure was missing.
# These pin that trigger: every call carries a bound, an expired bound raises,
# and the prompt is probed for before anything is compiled or written.


def test_every_call_carries_its_declared_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = _fake_tools(monkeypatch, authority=None)

    lifecycle.build_and_sign()
    assert recorded  # an unbounded call site would already have KeyError'd in the fake
    for call in recorded:
        expected = (
            lifecycle._ACL_PROBE_TIMEOUT_S
            if call.argv[:2] == ["codesign", "--sign"]
            else lifecycle._TIMEOUTS_S[call.argv[0]]
        )
        assert call.timeout == expected, call.argv


def test_swiftc_hang_fails_the_build_instead_of_blocking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = _fake_tools(monkeypatch, authority=None, hang=("swiftc",))

    with pytest.raises(lifecycle.PermissionsHelperTimeoutError) as err:
        lifecycle.build_and_sign()
    assert f"{lifecycle._TIMEOUTS_S['swiftc']:.0f}s" in str(err.value)
    assert not any(c[:2] == ["codesign", "--force"] for c in _argvs(recorded))


def test_codesign_hang_fails_with_the_acl_remedy_not_the_unlock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The 2026-08-02 hang, arriving after the preflight cleared this host. It has
    to surface as a failed step, and name the key's access control: a keychain
    unlock is not what fixes a per-use confirmation prompt."""
    from services.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    _fake_tools(monkeypatch, authority=None, hang=("codesign", "--force"))

    with pytest.raises(lifecycle.PermissionsHelperBuildError) as err:
        lifecycle.build_and_sign()
    assert "did not finish within" in str(err.value)
    assert "set-key-partition-list" in str(err.value)
    assert "unlock-keychain" not in str(err.value)


def test_acl_probe_precedes_the_build_and_signs_a_scratch_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.permissions_helper import lifecycle

    app = _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = _fake_tools(monkeypatch, authority=None)

    assert lifecycle.build_and_sign() == (app, True)
    argvs = _argvs(recorded)
    probe = next(c for c in argvs if c[:2] == ["codesign", "--sign"])
    assert probe[2] == lifecycle._CERT_CN
    # A scratch path, never the bundle: a probe that DOES trip the dialog must not
    # be able to leave the helper half-signed.
    assert str(app) not in probe[3]
    assert argvs.index(probe) < argvs.index(next(c for c in argvs if c[0] == "swiftc"))


def test_acl_prompt_refuses_before_anything_is_built(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = _fake_tools(monkeypatch, authority=None, hang=("codesign", "--sign"))

    with pytest.raises(lifecycle.PermissionsHelperBuildError) as err:
        lifecycle.build_and_sign()
    assert "SecurityAgent" in str(err.value)
    assert "set-key-partition-list" in str(err.value)
    assert not any(c[0] == "swiftc" for c in _argvs(recorded))
    assert not any(c[:2] == ["codesign", "--force"] for c in _argvs(recorded))


def test_acl_probe_nonzero_is_inconclusive_and_the_build_proceeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """codesign takes different paths for a loose file and a bundle, so a non-zero
    probe is no evidence the real sign would fail -- refusing on it would ground
    hosts that sign fine. The bound on the real sign is the backstop."""
    from services.permissions_helper import lifecycle

    app = _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = _fake_tools(monkeypatch, authority=None, acl_probe_rc=1)

    assert lifecycle.build_and_sign() == (app, True)
    assert _sign_command(recorded)[3] == lifecycle._CERT_CN


def test_a_hung_probe_answers_no_rather_than_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probes answer yes/no questions and their callers have no branch for an
    exception -- a query that hung is the "no" they already handle, with the
    timeout named in the one reason string an operator reads."""
    import subprocess

    from services.permissions_helper import lifecycle

    def hang(cmd, **kwargs):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(lifecycle, "run_bounded", hang)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(lifecycle, "_domain", lambda: "gui/501")
    monkeypatch.setattr("shared.paths.ava_home", lambda: Path("/x/.ava-demo"))

    assert lifecycle._is_loaded() is False
    assert lifecycle._signed_with_stable_cert(Path("/nope.app")) is False
    reason = lifecycle._keychain_lock_reason()
    assert reason is not None
    assert "timed out" in reason


# --- Old-layout job retirement -------------------------------------------
# Labels used to be the fixed `com.ava.permissions-helper.main`; after
# per-cluster home-slug labels arrived, converge wrote the slugged job but never
# retired a `main` job already loaded, so two KeepAlive jobs raced the same
# socket. The rename to permissions-helper added a second generation of
# leftovers: pre-rename `com.ava.native-helper.*` jobs pin the OLD socket file
# name (`native-helper.<port>.sock`) and the old env key, same port — they must
# be retired the same way.


def _write_agent_plist(
    agents: Path, label: str, sock: str, env_key: str = "AVA_NATIVE_HELPER_SOCKET"
) -> Path:
    p = agents / f"{label}.plist"
    p.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": ["/bin/true"],
                "EnvironmentVariables": {env_key: sock},
            }
        )
    )
    return p


def _retire_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, port: int = 9223) -> Path:
    """Point the retire machinery at a fake home: current socket, legacy socket
    derivation (run_dir + port), agents dir, launchd domain."""
    from services.permissions_helper import lifecycle

    agents = tmp_path / "LaunchAgents"
    agents.mkdir(parents=True)
    run = tmp_path / "run"
    run.mkdir(parents=True)
    monkeypatch.setattr(lifecycle, "_agents_dir", lambda: agents)
    monkeypatch.setattr(lifecycle, "_domain", lambda: "gui/501")
    monkeypatch.setattr(
        lifecycle, "permissions_helper_socket", lambda: run / f"permissions-helper.{port}.sock"
    )
    monkeypatch.setattr("shared.paths.run_dir", lambda: run)
    monkeypatch.setattr(lifecycle.settings.services, "permissions_helper_port", port)
    return agents


def test_old_main_job_bound_to_our_socket_is_retired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.permissions_helper import lifecycle

    agents = _retire_env(monkeypatch, tmp_path)
    sock = str(tmp_path / "run" / "permissions-helper.9223.sock")
    legacy_sock = str(tmp_path / "run" / "native-helper.9223.sock")

    mine = lifecycle._label()
    old_main = _write_agent_plist(agents, "com.ava.permissions-helper.main", legacy_sock)
    other_cluster = _write_agent_plist(
        agents,
        "com.ava.permissions-helper.ava-other-abcdef12",
        str(tmp_path / ".." / "x") + "/.ava-other/run/permissions-helper.18010.sock",
    )
    own = _write_agent_plist(agents, mine, sock, env_key="AVA_PERMISSIONS_HELPER_SOCKET")
    recorded: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        recorded.append(list(cmd))  # pyright: ignore[reportUnknownArgumentType]
        import subprocess

        return subprocess.CompletedProcess(cmd, 0, b"", b"")  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(lifecycle, "run_bounded", fake_run)  # pyright: ignore[reportUnknownArgumentType]

    lifecycle._retire_stale_jobs()

    # The racing old-layout job is gone (booted out + plist deleted)...
    assert not old_main.exists()
    assert recorded == [["launchctl", "bootout", "gui/501/com.ava.permissions-helper.main"]]
    # ...while other clusters' jobs and our own are untouched.
    assert other_cluster.exists()
    assert own.exists()


def test_pre_rename_native_helper_job_is_retired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A job written before the rename pins the OLD socket name + env key on the
    SAME port; after the rename it races nothing (its socket is dead) but it is
    a stale launchd job that must be booted out and deleted."""
    from services.permissions_helper import lifecycle

    agents = _retire_env(monkeypatch, tmp_path)
    legacy_sock = str(tmp_path / "run" / "native-helper.9223.sock")

    pre_rename = _write_agent_plist(agents, "com.ava.native-helper.ava-demo-1234abcd", legacy_sock)
    other = _write_agent_plist(
        agents,
        "com.ava.native-helper.ava-other-abcdef12",
        str(tmp_path / ".." / "y") + "/.ava-other/run/native-helper.18010.sock",
    )
    recorded: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        recorded.append(list(cmd))  # pyright: ignore[reportUnknownArgumentType]
        import subprocess

        return subprocess.CompletedProcess(cmd, 0, b"", b"")  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(lifecycle, "run_bounded", fake_run)  # pyright: ignore[reportUnknownArgumentType]

    lifecycle._retire_stale_jobs()

    assert not pre_rename.exists()
    assert recorded == [["launchctl", "bootout", "gui/501/com.ava.native-helper.ava-demo-1234abcd"]]
    # Another cluster's pre-rename job pins ITS OWN socket — untouched.
    assert other.exists()


def test_retire_is_idempotent_when_job_already_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.permissions_helper import lifecycle

    agents = _retire_env(monkeypatch, tmp_path)
    legacy_sock = str(tmp_path / "run" / "native-helper.9223.sock")

    _write_agent_plist(agents, "com.ava.permissions-helper.main", legacy_sock)
    monkeypatch.setattr(
        lifecycle,
        "run_bounded",
        lambda cmd, **_kw: __import__("subprocess").CompletedProcess(  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
            cmd, 78, b"", b""
        ),  # bootout: job not loaded
    )

    lifecycle._retire_stale_jobs()  # must not raise on bootout failure
    assert not (agents / "com.ava.permissions-helper.main.plist").exists()


def test_retire_removes_the_legacy_socket_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A booted-out job leaves its socket file behind; the retire step drops the
    pre-rename name so a stale dead socket never shadows the live one."""
    from services.permissions_helper import lifecycle

    _retire_env(monkeypatch, tmp_path)
    legacy_sock = tmp_path / "run" / "native-helper.9223.sock"
    legacy_sock.write_bytes(b"")
    monkeypatch.setattr(
        lifecycle,
        "run_bounded",
        lambda cmd, **_kw: __import__("subprocess").CompletedProcess(cmd, 0, b"", b""),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    lifecycle._retire_stale_jobs()
    assert not legacy_sock.exists()


def test_unrelated_cluster_plists_are_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.permissions_helper import lifecycle

    agents = _retire_env(monkeypatch, tmp_path)
    sock = str(tmp_path / "run" / "permissions-helper.9223.sock")

    other = _write_agent_plist(
        agents,
        "com.ava.permissions-helper.ava-demo-1234abcd",
        str(tmp_path / ".." / "z") + "/.ava-demo/run/permissions-helper.1234.sock",
        env_key="AVA_PERMISSIONS_HELPER_SOCKET",
    )
    monkeypatch.setattr(lifecycle, "permissions_helper_socket", lambda: Path(sock))

    assert lifecycle._stale_plists() == []
    assert other.exists()


# --- Windows named-pipe transport -----------------------------------------
# The pipe path is unreachable on Linux CI, so it is pinned with fakes: a
# stubbed _connect_pipe/_pipe_read and os.name forced to "nt". The shared
# _parse_reply contract is exercised by both transports' tests.


def test_pipe_transport_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.permissions_helper import client

    monkeypatch.setattr(client, "_IS_WINDOWS", True)
    seen: list[bytes] = []
    reply = b'{"id":1,"ok":true,"result":{"pong":true}}\n'
    chunks = [reply[i : i + 3] for i in range(0, len(reply), 3)]

    class _FakePipe:
        def write(self, data: bytes) -> None:
            seen.append(data)

        def flush(self) -> None:
            pass

        def close(self) -> None:
            pass

    def fake_connect(name: str = "ava-permissions-helper"):
        assert name == "ava-permissions-helper"
        return _FakePipe(), object()

    def fake_read(handle: object, deadline: float) -> bytes:
        return chunks.pop(0) if chunks else b""

    monkeypatch.setattr("services.permissions_helper._win_pipe.connect", fake_connect)
    monkeypatch.setattr("services.permissions_helper._win_pipe.read_available", fake_read)

    assert client.ping() == {"pong": True}
    assert json.loads(seen[0])["method"] == "ping"


def test_win_pipe_full_path_has_single_backslashes() -> None:
    """BUG-1 regression: pipe_path must produce \\.\\pipe\\<name> — a doubled
    backslash after the pipe name made WaitNamedPipeW fail with
    ERROR_BADPATHNAME (161) on the real Windows box."""
    from services.permissions_helper import _win_pipe

    assert _win_pipe.pipe_path("ava-permissions-helper") == r"\\.\pipe\ava-permissions-helper"
    assert _win_pipe.pipe_path().endswith(r"\ava-permissions-helper")


def test_pipe_transport_uses_shared_reply_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty / truncated / error replies raise through the same _parse_reply the
    socket path uses — the two transports cannot drift apart."""
    from services.permissions_helper import client

    for buf, expect in [
        (b"", "closed without a response"),
        (b'{"id":1,"ok":tr', "truncated"),
        (b'{"id":1,"ok":false,"error":"nope"}\n', "nope"),
    ]:
        try:
            client._parse_reply(buf, "ping")
            raise AssertionError(f"reply {buf!r} must raise")
        except client.PermissionsHelperError as e:
            assert expect in str(e)
