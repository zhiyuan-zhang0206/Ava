"""Permissions helper cases: root keeper method requests and results."""

from __future__ import annotations

import base64
import json
import os
import plistlib
import socket
import threading
import time
from pathlib import Path

import pytest

from base import paths
from base.config import settings
from base.host.converge.accessibility import AccessibilityState
from base.host.converge.screen_capture import ScreenCaptureState
from base.host.permissions_helper import client
from base.host.permissions_helper.client import PermissionsHelperClient, PermissionsHelperError
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import DARWIN_ARM64, LINUX_X86, fake_host
from services.desktop.permissions_helper.tests.test_permissions_helper import (
    FakeHelper,
    fake_tools,
    is_whitelisted_file_path,
    parse_strings,
    ping_reply,
    read_line,
    recorded_argvs,
    sign_command,
    stage_bundle,
    test_dr,
    write_current_build_state,
)
from services.desktop.permissions_helper.tests.test_permissions_helper import (
    fake_helper as fake_helper,
)


def test_root_keeper_method_requests_and_results(fake_helper) -> None:
    seen: list[dict[str, object]] = []
    results: dict[str, dict[str, object]] = {
        "root_seed": {
            "state": "running",
            "seeded": True,
            "restarts": 0,
            "stop_requested": False,
            "pid": 5150,
        },
        "root_status": {
            "state": "conflict",
            "seeded": True,
            "restarts": 1,
            "stop_requested": False,
            "conflict": {"pid": 4090, "since": 1_700_000_000.0},
        },
        "root_stop": {
            "state": "stopping",
            "seeded": True,
            "restarts": 1,
            "stop_requested": True,
            "pid": 5150,
        },
        "helper_shutdown": {"stopping": True, "pid": 4242, "run_dir": "/opt/ava/run"},
    }

    def handler(req: dict) -> dict:
        seen.append({key: value for key, value in req.items() if key != "id"})  # pyright: ignore[reportUnknownMemberType]
        return {"id": req["id"], "ok": True, "result": results[req["method"]]}  # pyright: ignore[reportUnknownArgumentType]

    path = fake_helper(handler)
    config: client.RootSeedConfig = {
        "argv": ["/opt/ava/.venv/bin/python", "-m", "services.supervision.ava_root"],
        "cwd": "/opt/ava",
        "run_dir": "/opt/ava/run",
        "stdout": "/opt/ava/logs/root.out.log",
        "stderr": "/opt/ava/logs/root.err.log",
        "env": {"AVA_TEST": "1"},
    }

    seeded = PermissionsHelperClient(path).seed_root(config)  # pyright: ignore[reportUnknownArgumentType]
    assert seeded["state"] == "running"
    assert seeded.get("pid") == 5150
    assert seen[0] == {"method": "root_seed", "config": config}

    status = PermissionsHelperClient(path).root_status()  # pyright: ignore[reportUnknownArgumentType]
    assert status["state"] == "conflict"
    assert status.get("conflict") == {"pid": 4090, "since": 1_700_000_000.0}
    assert seen[1] == {"method": "root_status"}

    stopped = PermissionsHelperClient(path).stop_root()  # pyright: ignore[reportUnknownArgumentType]
    assert stopped["state"] == "stopping"
    assert stopped["stop_requested"] is True
    assert seen[2] == {"method": "root_stop"}
    shutdown = PermissionsHelperClient(path).shutdown_helper(Path("/opt/ava/run"))  # pyright: ignore[reportUnknownArgumentType]
    assert shutdown == {"stopping": True, "pid": 4242, "run_dir": "/opt/ava/run"}
    assert seen[3] == {"method": "helper_shutdown", "run_dir": "/opt/ava/run"}


def test_file_method_mapping_and_list_result(fake_helper) -> None:
    seen: list[str] = []
    entries = [{"name": "notes.txt", "size": 12, "mtime": 1_725_000_000, "is_dir": False}]

    def handler(req: dict) -> dict:
        seen.append(req["method"])  # pyright: ignore[reportUnknownArgumentType]
        return {
            "id": req["id"],
            "ok": True,
            "result": {"entries": entries},
        }

    path = fake_helper(handler)
    assert PermissionsHelperClient(path).list_dir("/Users/ava/Downloads") == entries  # pyright: ignore[reportUnknownArgumentType]
    assert seen == ["file_list"]


@pytest.mark.parametrize("content", [b"", b"\x00binary\xff\n"])
def test_read_file_decodes_base64_content(fake_helper, content: bytes) -> None:
    seen: list[str] = []

    def handler(req: dict) -> dict:
        seen.append(req["method"])  # pyright: ignore[reportUnknownArgumentType]
        return {
            "id": req["id"],
            "ok": True,
            "result": {"content_b64": base64.b64encode(content).decode("ascii")},
        }

    path = fake_helper(handler)
    assert PermissionsHelperClient(path).read_file("/Users/ava/Downloads/example.bin") == content  # pyright: ignore[reportUnknownArgumentType]
    assert seen == ["file_read"]


@pytest.mark.parametrize("result", [{}, {"content_b64": 1}, {"content_b64": "%%%"}])
def test_read_file_rejects_missing_or_malformed_content(
    fake_helper, result: dict[str, object]
) -> None:
    def handler(req: dict) -> dict:
        return {"id": req["id"], "ok": True, "result": result}

    with pytest.raises(PermissionsHelperError, match="invalid file_read response"):
        PermissionsHelperClient(fake_helper(handler)).read_file("/Users/ava/Downloads/example.bin")  # pyright: ignore[reportUnknownArgumentType]


def test_file_operations_raise_server_errors(fake_helper) -> None:
    def handler(req: dict) -> dict:
        error = "outside whitelist" if req["method"] == "file_list" else "file too large"
        return {"id": req["id"], "ok": False, "error": error}

    path = fake_helper(handler)
    with pytest.raises(PermissionsHelperError, match="outside whitelist"):
        PermissionsHelperClient(path).list_dir("/private/secret")  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(PermissionsHelperError, match="file too large"):
        PermissionsHelperClient(path).read_file("/Users/ava/Downloads/large.bin")  # pyright: ignore[reportUnknownArgumentType]


def test_file_whitelist_boundary_reference(tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    desktop = tmp_path / "Desktop"
    incoming = tmp_path / ".ava" / "incoming"
    for root in (downloads, desktop, incoming):
        root.mkdir(parents=True)

    nested_file = downloads / "nested" / "report.txt"
    nested_file.parent.mkdir()
    nested_file.write_text("approved")
    sibling_prefix = tmp_path / "DownloadsEvil"
    sibling_prefix.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("no")
    (downloads / "escape").symlink_to(outside, target_is_directory=True)

    roots = [downloads, desktop, incoming]
    assert is_whitelisted_file_path(nested_file, roots)
    assert is_whitelisted_file_path(nested_file.parent, roots)
    assert not is_whitelisted_file_path(sibling_prefix / "report.txt", roots)
    assert not is_whitelisted_file_path(downloads / "escape" / "secret.txt", roots)
    assert not is_whitelisted_file_path(Path("Downloads/report.txt"), roots)


def test_gui_ops_wire_requests(fake_helper) -> None:
    # Pin the wire request each GUI-driver wrapper emits (method name + args),
    # so a driver script can rely on these exact shapes.
    seen: list[dict] = []

    def handler(req: dict) -> dict:
        seen.append({k: v for k, v in req.items() if k != "id"})  # pyright: ignore[reportUnknownMemberType]
        return {"id": req["id"], "ok": True, "result": {}}

    path = fake_helper(handler)
    PermissionsHelperClient(path).key(76)  # pyright: ignore[reportUnknownArgumentType]
    PermissionsHelperClient(path).key(9, cmd=True)  # pyright: ignore[reportUnknownArgumentType]
    PermissionsHelperClient(path).scroll(10, 20, -50)  # pyright: ignore[reportUnknownArgumentType]
    PermissionsHelperClient(path).window_info("Notes")  # pyright: ignore[reportUnknownArgumentType]
    PermissionsHelperClient(path).session_info()  # pyright: ignore[reportUnknownArgumentType]
    PermissionsHelperClient(path).click(5, 6, double=True)  # pyright: ignore[reportUnknownArgumentType]
    assert seen == [
        {"method": "key", "code": 76, "cmd": False},
        {"method": "key", "code": 9, "cmd": True},
        {"method": "scroll", "x": 10, "y": 20, "dy": -50},
        {"method": "window_info", "owner": "Notes"},
        {"method": "session_info"},
        {"method": "click", "x": 5, "y": 6, "double": True},
    ]


def test_incapability_branch_ordering(monkeypatch: pytest.MonkeyPatch) -> None:
    # Drive every branch explicitly (a macOS host with the tools it has, then a Linux one) so a
    # reordered or dropped prong is caught whatever machine the test runs on.
    from base.host.system import probes as pp

    def mac(programs: dict[str, str]) -> Host:
        return fake_host(platform=DARWIN_ARM64, programs=programs)

    def reason(host: Host) -> str:
        r = pp.permissions_helper_incapability(host=host)
        assert r is not None
        return r

    present = {"swiftc": "/usr/bin/swiftc", "codesign": "/usr/bin/codesign"}

    def fake_display_available(*, host: Host) -> bool:
        return True

    monkeypatch.setattr(pp, "display_available", fake_display_available)

    assert pp.permissions_helper_incapability(host=mac(present)) is None
    assert "no swiftc" in reason(mac({}))
    assert "no codesign" in reason(mac({"swiftc": "/usr/bin/swiftc"}))

    def fake_display_available2(*, host: Host) -> bool:
        return False

    monkeypatch.setattr(pp, "display_available", fake_display_available2)
    assert "no display" in reason(mac(present))

    assert "macOS only" in reason(fake_host(platform=LINUX_X86))


def test_recv_reassembles_across_chunks(fake_helper) -> None:
    def raw(conn: socket.socket) -> None:
        read_line(conn)
        payload = json.dumps({"id": 1, "ok": True, "result": {"pong": True}}).encode() + b"\n"
        conn.sendall(payload[:5])
        time.sleep(0.05)
        conn.sendall(payload[5:])

    assert PermissionsHelperClient(fake_helper(raw=raw)).ping() == {"pong": True}  # pyright: ignore[reportUnknownArgumentType]


def test_closed_without_response_raises(fake_helper) -> None:
    with pytest.raises(PermissionsHelperError, match="closed without a response"):
        PermissionsHelperClient(
            fake_helper(raw=read_line)  # pyright: ignore[reportUnknownArgumentType]
        ).ping()  # read, then close, no reply


def test_truncated_response_raises(fake_helper) -> None:
    def raw(conn: socket.socket) -> None:
        read_line(conn)
        conn.sendall(b'{"id":1,"ok":tr')  # partial JSON, no trailing newline

    with pytest.raises(PermissionsHelperError, match="truncated"):
        PermissionsHelperClient(fake_helper(raw=raw)).ping()  # pyright: ignore[reportUnknownArgumentType]


def test_call_timeout_raises(fake_helper, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "CALL_TIMEOUT_S", 0.2)

    def raw(conn: socket.socket) -> None:
        read_line(conn)
        time.sleep(1.0)  # accept, never reply

    with pytest.raises(PermissionsHelperError, match="did not respond"):
        PermissionsHelperClient(fake_helper(raw=raw)).ping()  # pyright: ignore[reportUnknownArgumentType]


def test_line_limit_guard(fake_helper, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "LINE_LIMIT", 16)

    def raw(conn: socket.socket) -> None:
        read_line(conn)
        conn.sendall(b"x" * 64)  # oversized, newline-less

    with pytest.raises(PermissionsHelperError, match="exceeded line limit"):
        PermissionsHelperClient(fake_helper(raw=raw)).ping()  # pyright: ignore[reportUnknownArgumentType]


def test_connect_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    # Server binds only after the first connect attempt fails -> exercises the
    # retry loop that exists for the daemon-startup race.
    monkeypatch.setattr(client, "CONNECT_DELAY_S", 0.1)
    path = f"/tmp/avah-retry{os.getpid()}.sock"  # noqa: S108 — short path for AF_UNIX
    Path(path).unlink(missing_ok=True)
    holder: list[FakeHelper] = []

    def late_start() -> None:
        time.sleep(0.25)
        holder.append(
            FakeHelper(path, lambda req: {"id": req["id"], "ok": True, "result": {"pong": True}})
        )

    t = threading.Thread(target=late_start, daemon=True)
    t.start()
    try:
        assert PermissionsHelperClient(path).ping() == {"pong": True}
    finally:
        t.join(timeout=2)
        for s in holder:
            s.close()
        Path(path).unlink(missing_ok=True)


def test_label_is_per_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    from base.cluster import home_slug
    from services.desktop.permissions_helper import lifecycle

    monkeypatch.setattr(paths, "ava_home", lambda: Path("/x/.ava-demo"))
    assert lifecycle.label() == f"com.ava.permissions-helper.{home_slug(Path('/x/.ava-demo'))}"


def test_screen_capture_probe_reads_the_helpers_grant(fake_helper) -> None:
    seen: list[str] = []

    def handler(req: dict) -> dict:
        seen.append(req["method"])  # pyright: ignore[reportUnknownArgumentType]
        return ping_reply(True)(req)

    status = PermissionsHelperClient(fake_helper(handler)).check_screen_capture()  # pyright: ignore[reportUnknownArgumentType]
    assert seen == ["ping"]  # measured in the helper, not in whoever is asking
    assert status.state is ScreenCaptureState.AVAILABLE
    assert status.available is True


def test_screen_capture_probe_reports_no_grant(fake_helper) -> None:
    status = PermissionsHelperClient(fake_helper(ping_reply(False))).check_screen_capture()  # pyright: ignore[reportUnknownArgumentType]
    assert status.state is ScreenCaptureState.NO_GRANT
    assert status.available is False
    # The fix is authorizing the helper, not restarting the cluster from a local
    # terminal -- the SSH/terminal story never applied to the helper's own grant.
    assert "AvaPermissionsHelper" in status.diagnostic
    assert "System Settings" in status.diagnostic
    assert "Terminal.app" not in status.diagnostic


def test_screen_capture_probe_keeps_unreachable_distinct_from_denied() -> None:
    status = PermissionsHelperClient("/tmp/ava-native-does-not-exist.sock").check_screen_capture(  # noqa: S108 — nonexistent by design
        settle_s=0.0
    )
    assert status.state is ScreenCaptureState.HELPER_UNREACHABLE
    assert status.available is False
    # An unread grant is not a missing one: this fault is fixed at launchd, so
    # the message must not send the operator to the permissions pane.
    assert "launchctl" in status.diagnostic
    assert "System Settings" not in status.diagnostic


class _ColdHelper(PermissionsHelperClient):
    """The first ping finds the helper unreachable, as when launchd has only just bootstrapped it."""

    def __init__(self) -> None:
        super().__init__("/unused.sock")
        self.calls = 0

    def ping(self):  # pyright: ignore[reportIncompatibleMethodOverride]
        self.calls += 1
        if self.calls == 1:
            raise PermissionsHelperError("permissions helper not reachable")
        return {"pong": True, "preflight_screen": True, "ax_trusted": True}


def test_screen_capture_probe_waits_out_a_cold_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    # launchd may have only just bootstrapped the helper, so one unreachable ping
    # inside the settle window is a cold start rather than a dead daemon.
    helper = _ColdHelper()
    monkeypatch.setattr(client, "PROBE_RETRY_DELAY_S", 0.01)
    status = helper.check_screen_capture(settle_s=2.0)
    assert helper.calls == 2
    assert status.state is ScreenCaptureState.AVAILABLE


def test_accessibility_probe_reads_the_helpers_grant(fake_helper) -> None:
    status = PermissionsHelperClient(fake_helper(ping_reply(True, True))).check_accessibility()  # pyright: ignore[reportUnknownArgumentType]
    assert status.state is AccessibilityState.GRANTED
    assert status.available is True


def test_accessibility_probe_reports_missing_grant(fake_helper) -> None:
    status = PermissionsHelperClient(fake_helper(ping_reply(True, False))).check_accessibility()  # pyright: ignore[reportUnknownArgumentType]
    assert status.state is AccessibilityState.NOT_GRANTED
    assert status.available is False
    assert "System Settings" in status.diagnostic
    assert "Accessibility" in status.diagnostic


def test_accessibility_probe_keeps_unreachable_distinct_from_missing_grant() -> None:
    status = PermissionsHelperClient("/tmp/ava-native-does-not-exist.sock").check_accessibility(  # noqa: S108 — nonexistent by design
        settle_s=0.0
    )
    assert status.state is AccessibilityState.HELPER_UNREACHABLE
    assert status.available is False
    assert "launchctl" in status.diagnostic
    assert "System Settings" not in status.diagnostic


def test_info_plist_allows_ui_for_panel_mode() -> None:
    """No LSBackgroundOnly: a socket-less launch shows the panel (design v1 Phase A)."""
    plist_path = (
        Path(__file__).resolve().parents[5]
        / "services"
        / "desktop"
        / "permissions_helper"
        / "helper"
        / "Info.plist"
    )
    with plist_path.open("rb") as handle:
        plist = plistlib.load(handle)
    assert plist.get("LSBackgroundOnly") is not True
    assert plist.get("LSUIElement") is True


def test_panel_locale_catalogs_are_symmetric() -> None:
    """en (base) and zh-Hans panel catalogs carry identical key sets; en is ASCII."""
    locales = (
        Path(__file__).resolve().parents[5]
        / "services"
        / "desktop"
        / "permissions_helper"
        / "helper"
        / "locales"
    )
    en = parse_strings(locales / "en.lproj" / "Localizable.strings")
    zh = parse_strings(locales / "zh-Hans.lproj" / "Localizable.strings")
    assert en
    assert set(en) == set(zh)
    assert all(value.isascii() for value in en.values())
    assert all(value for value in zh.values())


def test_panel_fill_runs_carry_the_cli_user_present_guard() -> None:
    """The fill button's run appends --fill-pending with its required
    --confirm-user-present attestation; checks never request a fill."""
    source = (
        Path(__file__).parents[5] / "services/desktop/permissions_helper/helper/main.swift"
    ).read_text()
    run_tool = source.split("private func runTool(check: Bool)", 1)[1].split(
        "private func runFinished()", 1
    )[0]
    code_only = "\n".join(line.split("//", 1)[0] for line in run_tool.splitlines())
    compact = " ".join(code_only.split())

    # The guard pair rides the non-check branch together -- a check run can
    # never request a fill (zero dialogs).
    assert (
        'if check { arguments.append("--check") } else {'
        ' arguments.append("--fill-pending")'
        ' arguments.append("--confirm-user-present") }'
    ) in compact

    # Zero automatic paths: the one fill call site is the fix button; the
    # launch-time run and the refresh button stay checks.
    assert source.count("runTool(check: false)") == 1
    fix_tapped = source.split("@objc private func fixTapped()", 1)[1].split("}", 1)[0]
    assert "runTool(check: false)" in fix_tapped
    launch = source.split("func applicationDidFinishLaunching", 1)[1].split(
        "func applicationShouldTerminateAfterLastWindowClosed", 1
    )[0]
    assert "runTool(check: true)" in launch


def test_read_dr_accepts_legacy_stderr_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """codesign once emitted the DR line on stderr; the reader searches both."""
    import subprocess

    from services.desktop.permissions_helper import lifecycle

    app = tmp_path / "AvaPermissionsHelper.app"

    def run(cmd, **kwargs):
        if list(cmd)[:3] == ["codesign", "-d", "-r-"]:  # pyright: ignore[reportUnknownArgumentType]
            return subprocess.CompletedProcess(
                cmd,  # pyright: ignore[reportUnknownArgumentType]
                0,
                b"",
                f"designated => {test_dr()}\n".encode(),
            )
        raise AssertionError(f"unexpected call {cmd}")

    monkeypatch.setattr(lifecycle, "run_bounded", run)  # pyright: ignore[reportUnknownArgumentType]
    assert lifecycle.read_dr(app) == test_dr()


def test_current_stable_signed_bundle_is_not_resigned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Re-signing a current binary changes its cdhash and drops the grants it
    already holds, so the skip-if-up-to-date gate has to survive intact."""
    from services.desktop.permissions_helper import lifecycle

    app = stage_bundle(monkeypatch, tmp_path, exe_present=True)
    recorded = fake_tools(monkeypatch, authority=lifecycle.CERT_CN)
    write_current_build_state(app, lifecycle.source_content_hash())

    assert lifecycle.build_and_sign() == (app, False)
    assert not any(c[0] == "swiftc" for c in recorded_argvs(recorded))
    assert not any(c[:2] == ["codesign", "--force"] for c in recorded_argvs(recorded))


def test_keychain_path_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_PERMISSIONS_HELPER_KEYCHAIN routes signing to a CI-owned keychain."""
    from services.desktop.permissions_helper import lifecycle

    monkeypatch.setattr(
        settings.services,
        "permissions_helper_keychain",
        "/tmp/ci-signing.keychain-db",  # noqa: S108
    )
    assert lifecycle.keychain_path() == Path("/tmp/ci-signing.keychain-db")  # noqa: S108
    monkeypatch.setattr(settings.services, "permissions_helper_keychain", None)
    assert lifecycle.keychain_path() == Path.home() / "Library" / "Keychains" / "login.keychain-db"


def test_signing_cert_import_uses_supported_security_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The macOS security import command must contain only supported flags."""
    from services.desktop.permissions_helper import lifecycle

    keychain = tmp_path / "ci-signing.keychain-db"
    monkeypatch.setattr(lifecycle, "keychain_path", lambda: keychain)
    recorded = fake_tools(monkeypatch, authority=None, identity_output="0 identities found\n")

    lifecycle.ensure_signing_cert()

    import_argv = next(
        argv for argv in recorded_argvs(recorded) if argv[:2] == ["security", "import"]
    )
    assert import_argv[:2] == ["security", "import"]
    assert import_argv[2].endswith("ident.p12")
    assert import_argv[3:] == [
        "-k",
        str(keychain),
        "-P",
        "ava",
        "-T",
        "/usr/bin/codesign",
        "-A",
    ]


def test_locked_keychain_does_not_fail_a_current_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only a real rebuild needs the signing key, so an SSH host whose helper is
    already current converges without ever consulting the keychain -- or the key
    ACL, the other pre-sign check that reaches for it."""
    from services.desktop.permissions_helper import lifecycle

    app = stage_bundle(monkeypatch, tmp_path, exe_present=True)
    recorded = fake_tools(monkeypatch, authority=lifecycle.CERT_CN, keychain_rc=1)
    write_current_build_state(app, lifecycle.source_content_hash())

    assert lifecycle.build_and_sign() == (app, False)
    assert not any(c[:2] == ["security", "show-keychain-info"] for c in recorded_argvs(recorded))
    assert not any(c[:2] == ["codesign", "--sign"] for c in recorded_argvs(recorded))


def test_current_stable_signed_bundle_skips_signing_smoke(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = stage_bundle(monkeypatch, tmp_path, exe_present=True)
    fake_tools(monkeypatch, authority=lifecycle.CERT_CN)
    write_current_build_state(app, lifecycle.source_content_hash())
    smoke_calls: list[None] = []
    monkeypatch.setattr(lifecycle, "preflight_signing_smoke", lambda: smoke_calls.append(None))

    assert lifecycle.build_and_sign() == (app, False)
    assert smoke_calls == []


def test_fresh_build_signs_with_the_stable_certificate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = fake_tools(monkeypatch, authority=None)

    assert lifecycle.build_and_sign() == (app, True)
    assert sign_command(recorded) == [
        "codesign",
        "--force",
        "--sign",
        lifecycle.CERT_CN,
        # Hardened runtime: dyld ignores DYLD_* for the helper (review P2-A).
        "--options",
        "runtime",
        # AppleEvents: the entitlement tccd needs to build an attribution
        # chain for helper-spawned osascript/AE children.
        "--entitlements",
        str(lifecycle.ENTITLEMENTS),
        "--identifier",
        lifecycle.HELPER_BUNDLE_ID,
        "--requirements",
        f"=designated => {test_dr()}",
        str(app),
    ]
    state = json.loads((app.parent / "build-state.json").read_text())
    assert state["source_hash"] == lifecycle.source_content_hash()
    assert state["dr"] == test_dr()


def test_entitlements_request_apple_events_for_spawned_children() -> None:
    """Exactly the AppleEvents entitlement, in the file production signs with.

    The helper is the TCC attribution ancestor of what it spawns: with this
    entitlement a helper-spawned osascript/AE child reaches a normal Automation
    dialog, and without it tccd refuses the chain and the request dies as
    -1712 (exec) / -609 (pty) before any prompt can appear."""
    entitlements_path = (
        Path(__file__).resolve().parents[5]
        / "services"
        / "desktop"
        / "permissions_helper"
        / "helper"
        / "helper.entitlements"
    )
    with entitlements_path.open("rb") as handle:
        entitlements = plistlib.load(handle)
    assert entitlements == {"com.apple.security.automation.apple-events": True}


def test_source_mtime_change_does_not_rebuild_identical_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = stage_bundle(monkeypatch, tmp_path, exe_present=True)
    recorded = fake_tools(monkeypatch, authority=lifecycle.CERT_CN)
    write_current_build_state(app, lifecycle.source_content_hash())
    exe = app / "Contents" / "MacOS" / "AvaPermissionsHelper"
    newer = exe.stat().st_mtime + 60
    os.utime(lifecycle.SOURCE, (newer, newer))

    assert lifecycle.build_and_sign() == (app, False)
    assert not any(c[0] == "swiftc" for c in recorded_argvs(recorded))
