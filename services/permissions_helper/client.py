"""Client for the permissions helper daemon (macOS + Windows).

Connects to this cluster's helper and exchanges one line-delimited JSON
request/response per call: a Unix socket on macOS/Linux, a named pipe
(``\\\\.\\pipe\\ava-permissions-helper``) on Windows. Same wire contract both. Skills that drive the macOS
desktop (screen capture, clicks, keystrokes, window geometry) call these
functions instead of shelling out to screencapture / posting CGEvents
themselves -- so the privileged, permission-granted work happens in the one
signed helper process, not in every caller.

Because the helper is that single process, it is also the only place the
desktop permission grants can be read from; `check_screen_capture` and
`check_accessibility` are the interpreted calls here, turning a `ping` into the
statuses the converge preflight reports.
"""

from __future__ import annotations

import base64
import binascii
import itertools
import json
import os
import socket
import time
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from shared.accessibility import AccessibilityState, AccessibilityStatus
from shared.paths import permissions_helper_socket
from shared.screen_capture import ScreenCaptureState, ScreenCaptureStatus

# Transport selection: named pipe on Windows, Unix socket elsewhere. A module
# constant (not a live os.name check) so tests can flip the transport without
# changing the process-wide platform (pathlib keys off os.name).
_IS_WINDOWS = os.name == "nt"

_LINE_LIMIT = (
    64 * 1024 * 1024
)  # client-side cap on one response line; metadata stays small but keep headroom
_CONNECT_ATTEMPTS = 5
_CONNECT_DELAY_S = 0.2
_CALL_TIMEOUT_S = (
    30.0  # bound a connected read so a stalled helper surfaces as an error, not a hang
)
# A helper launchd has only just bootstrapped may not have bound its socket yet.
# Keep retrying the preflight ping for this long so a cold start is not reported
# as a dead helper -- a false alarm is the exact failure this probe replaced.
_PROBE_SETTLE_S = 5.0
_PROBE_RETRY_DELAY_S = 0.25  # a ping can also fail instantly, so pace the retry
_ids = itertools.count(1)


class PermissionsHelperError(RuntimeError):
    """A permissions helper call failed, or the daemon was unreachable."""


def _connect(path: str) -> socket.socket:
    last: OSError | None = None
    for _ in range(_CONNECT_ATTEMPTS):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(path)
            return s
        except (FileNotFoundError, ConnectionRefusedError) as e:
            last = e
            s.close()
            time.sleep(_CONNECT_DELAY_S)
    raise PermissionsHelperError(f"permissions helper not reachable at {path}: {last}")


def _call(
    method: str,
    *,
    sock_path: str | Path | None = None,
    _disconnect_is_success: bool = False,
    **args: object,
) -> Any:
    """One JSON-line request/response over the platform transport.

    POSIX dials this cluster's Unix socket; Windows dials the machine-wide
    named pipe (``ava-permissions-helper``) the user-session helper listens
    on. Both speak the same wire contract; a helper that never answers gets
    the same unreachable/truncated errors either way.
    """
    req = {"id": next(_ids), "method": method, **args}
    if _IS_WINDOWS:
        return _call_pipe(req, disconnect_is_success=_disconnect_is_success)
    path = str(sock_path or permissions_helper_socket())
    s = _connect(path)
    s.settimeout(_CALL_TIMEOUT_S)
    try:
        s.sendall((json.dumps(req) + "\n").encode())
        buf = bytearray()
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
            if len(buf) > _LINE_LIMIT:
                raise PermissionsHelperError("permissions helper response exceeded line limit")
    except TimeoutError as e:
        raise PermissionsHelperError(
            f"permissions helper did not respond to {method!r} within {_CALL_TIMEOUT_S}s"
        ) from e
    finally:
        s.close()
    if _disconnect_is_success and not buf:
        return True
    return _parse_reply(bytes(buf), method)


def _call_pipe(req: dict[str, object], *, disconnect_is_success: bool = False) -> Any:
    """Windows transport: named-pipe file I/O (see services.permissions_helper._win_pipe)."""
    from services.permissions_helper import _win_pipe

    conn = handle = None
    for _ in range(_CONNECT_ATTEMPTS):
        try:
            conn, handle = _win_pipe.connect()
            break
        except (ConnectionError, OSError):
            time.sleep(_CONNECT_DELAY_S)
    if conn is None:
        raise PermissionsHelperError(
            f"permissions helper not reachable at pipe {_win_pipe.PIPE_NAME!r}"
        )
    try:
        conn.write((json.dumps(req) + "\n").encode())
        conn.flush()
        deadline = time.monotonic() + _CALL_TIMEOUT_S
        buf = bytearray()
        while not buf.endswith(b"\n"):
            chunk = _win_pipe.read_available(handle, deadline)
            if not chunk:
                break
            buf += chunk
            if len(buf) > _LINE_LIMIT:
                raise PermissionsHelperError("permissions helper response exceeded line limit")
    finally:
        conn.close()
    if disconnect_is_success and not buf:
        return True
    return _parse_reply(bytes(buf), str(req["method"]))


def _parse_reply(buf: bytes, method: str) -> Any:
    """The wire reply contract, shared by the socket and pipe transports."""
    if not buf:
        raise PermissionsHelperError(f"permissions helper closed without a response to {method!r}")
    if not buf.endswith(b"\n"):
        raise PermissionsHelperError(f"permissions helper response to {method!r} was truncated")
    resp = json.loads(buf)
    # ok / error / result are contract-guaranteed by the daemon's dispatch; index
    # with [] so a wire-format break blows up here instead of being masked.
    if not resp["ok"]:
        raise PermissionsHelperError(resp["error"])
    return resp["result"]


# Per-method `result` shapes. Each mirrors the object the Swift daemon's matching
# handler returns (`services/permissions_helper/helper/main.swift`); they are the typed face
# of `_call`'s dynamic JSON so callers index fields, not a bare dict.


class PingResult(TypedDict):
    pong: bool
    preflight_screen: bool  # Screen Recording grant held
    ax_trusted: NotRequired[bool]  # Accessibility grant held (macOS only)


class ScreencaptureResult(TypedDict):
    path: str
    bytes: int  # PNG size on disk, or -1 if it could not be stat'd


class ClickPoint(TypedDict):
    x: float
    y: float


class ClickResult(TypedDict):
    clicked: ClickPoint
    double: bool


class TypeResult(TypedDict):
    typed: int  # characters sent


class KeyResult(TypedDict):
    key: int  # the virtual keycode pressed
    cmd: bool


class ScrollResult(TypedDict):
    scrolled: int  # dy pixels applied


class WindowGeometry(TypedDict):
    x: float
    y: float
    w: float
    h: float


class AxWindowInfo(WindowGeometry):
    app: str


class WindowInfo(WindowGeometry):
    owner: str


class SessionInfo(TypedDict):
    locked: bool
    on_console: bool


class SessionProc(TypedDict):
    name: str
    pid: int
    alive: bool


class SpawnResult(TypedDict):
    pid: int
    reused: bool


class SessionListResult(TypedDict):
    sessions: list[SessionProc]


class AliveResult(TypedDict):
    alive: bool


class SignalResult(TypedDict):
    sent: bool


class ScreenSize(TypedDict):
    x: float
    y: float
    w: float
    h: float
    scale: float  # backing scale factor; 1 when physical == logical (Windows)


class FrontmostApp(TypedDict):
    app: str  # display name, or "" when nothing is focused


class PermissionsFileEntry(TypedDict):
    name: str
    size: int
    mtime: int
    is_dir: bool


class FileListResult(TypedDict):
    entries: list[PermissionsFileEntry]


class FileReadResult(TypedDict):
    content_b64: str


def ping(*, sock_path: str | Path | None = None) -> PingResult:
    """Report the helper's liveness and whether it holds the desktop grants."""
    return _call("ping", sock_path=sock_path)


def screencapture_region(
    x: int, y: int, w: int, h: int, path: str, *, sock_path: str | Path | None = None
) -> ScreencaptureResult:
    """Capture the screen rectangle (x, y, w, h) to a PNG at `path`."""
    return _call("screencapture_region", x=x, y=y, w=w, h=h, path=path, sock_path=sock_path)


def list_dir(path: str, *, sock_path: str | Path | None = None) -> list[PermissionsFileEntry]:
    """List a whitelisted directory's immediate entries, sorted by name."""
    result: FileListResult = _call("file_list", path=path, sock_path=sock_path)
    return result["entries"]


def read_file(path: str, *, sock_path: str | Path | None = None) -> bytes:
    """Read a whitelisted regular file of at most 32 MiB."""
    result: FileReadResult = _call("file_read", path=path, sock_path=sock_path)
    try:
        content_b64 = result["content_b64"]
    except (KeyError, TypeError) as exc:
        raise PermissionsHelperError("invalid file_read response") from exc
    if not isinstance(content_b64, str):
        raise PermissionsHelperError("invalid file_read response")
    try:
        content = base64.b64decode(content_b64, validate=False)
    except (ValueError, binascii.Error) as exc:
        raise PermissionsHelperError("invalid file_read response") from exc
    if base64.b64encode(content).decode("ascii") != content_b64:
        raise PermissionsHelperError("invalid file_read response")
    return content


def click(
    x: float, y: float, *, double: bool = False, sock_path: str | Path | None = None
) -> ClickResult:
    """Click the left mouse button at the global screen point (x, y)."""
    return _call("click", x=x, y=y, double=double, sock_path=sock_path)


def type_text(text: str, *, sock_path: str | Path | None = None) -> TypeResult:
    """Type `text` as keyboard input into the focused field (handles CJK)."""
    return _call("type", text=text, sock_path=sock_path)


def key(code: int, *, cmd: bool = False, sock_path: str | Path | None = None) -> KeyResult:
    """Press the key with virtual keycode `code` (Windows VK code; cmd = Ctrl)."""
    return _call("key", code=code, cmd=cmd, sock_path=sock_path)


def scroll(x: float, y: float, dy: int, *, sock_path: str | Path | None = None) -> ScrollResult:
    """Move to (x, y) and scroll vertically by `dy` pixels (negative = older)."""
    return _call("scroll", x=x, y=y, dy=dy, sock_path=sock_path)


def ax_window_info(app: str, *, sock_path: str | Path | None = None) -> AxWindowInfo:
    """Report the on-screen geometry of `app`'s focused window via accessibility."""
    return _call("ax_window_info", app=app, sock_path=sock_path)


def window_info(owner: str, *, sock_path: str | Path | None = None) -> WindowInfo:
    """Report the geometry of `owner`'s normal on-screen window via the window list."""
    return _call("window_info", owner=owner, sock_path=sock_path)


def session_info(*, sock_path: str | Path | None = None) -> SessionInfo:
    """Report whether the login session is locked or off-console."""
    return _call("session_info", sock_path=sock_path)


def spawn_process(
    name: str,
    argv: list[str],
    env: dict[str, str],
    cwd: str,
    stdout: str,
    stderr: str,
    *,
    sock_path: str | Path | None = None,
) -> SpawnResult:
    """Spawn or reuse a named direct child of the permissions helper."""
    result: SpawnResult = _call(
        "spawn",
        name=name,
        argv=argv,
        env=env,
        cwd=cwd,
        stdout=stdout,
        stderr=stderr,
        sock_path=sock_path,
    )
    return result


def session_list(prefix: str = "", *, sock_path: str | Path | None = None) -> list[SessionProc]:
    """List helper-owned process sessions whose names start with `prefix`."""
    result: SessionListResult = _call("session_list", prefix=prefix, sock_path=sock_path)
    return result["sessions"]


def session_has(name: str, *, sock_path: str | Path | None = None) -> bool:
    """Report whether the named helper-owned process session is alive."""
    result: AliveResult = _call("session_has", name=name, sock_path=sock_path)
    return result["alive"]


def signal_session(
    *,
    name: str | None = None,
    pid: int | None = None,
    sig: int = 15,
    sock_path: str | Path | None = None,
) -> bool:
    """Send `sig` to exactly one named session or explicit pid."""
    if (name is None) == (pid is None):
        raise ValueError("exactly one of name or pid is required")
    if name is not None:
        result: SignalResult = _call("signal", name=name, sig=sig, sock_path=sock_path)
    else:
        result = _call("signal", pid=pid, sig=sig, sock_path=sock_path)
    return result["sent"]


def request_self_upgrade(exe_path: str, *, sock_path: str | Path | None = None) -> bool:
    """Ask the helper to exec a replacement; a clean disconnect means it succeeded."""
    return bool(
        _call(
            "self_upgrade",
            exe_path=exe_path,
            sock_path=sock_path,
            _disconnect_is_success=True,
        )
    )


def screen_size(*, sock_path: str | Path | None = None) -> ScreenSize:
    """Report the main display's geometry in logical points + backing scale.

    Computer-use callers use this to map screenshot pixels (physical) to
    click coordinates (logical): divide by `scale` (macOS Retina only;
    Windows reports scale 1)."""
    return _call("screen_size", sock_path=sock_path)


def frontmost_app(*, sock_path: str | Path | None = None) -> FrontmostApp:
    """Report the frontmost application's display name ("" when none)."""
    return _call("frontmost_app", sock_path=sock_path)


_NO_GRANT_DIAGNOSTIC = (
    "The permissions helper is running but holds no Screen Recording grant, so OS-level "
    "screen capture returns the wallpaper or a black image. This affects skills that "
    "capture the macOS desktop through the helper; browser screenshots are taken over "
    "Chrome DevTools and are unaffected. Fix: System Settings > Privacy & Security > "
    "Screen Recording, enable AvaPermissionsHelper, then restart its launchd job "
    "(`launchctl list | grep com.ava.permissions-helper` for the label, then "
    "`launchctl kickstart -k gui/$(id -u)/<label>`)."
)

_NO_AX_GRANT_DIAGNOSTIC = (
    "The permissions helper is running but holds no Accessibility grant, so macOS silently "
    "drops the synthetic clicks and keystrokes it posts (calls return success but the desktop "
    "never sees them). Screen Recording is a separate grant and is unaffected. Fix: System "
    "Settings > Privacy & Security > Accessibility, enable AvaPermissionsHelper — rebuilding "
    "or re-signing the helper resets this grant once. The grant applies to the running helper "
    "immediately; no launchd restart is needed."
)


def check_screen_capture(
    *, sock_path: str | Path | None = None, settle_s: float = _PROBE_SETTLE_S
) -> ScreenCaptureStatus:
    """Report whether OS-level screen capture works, by asking the helper.

    The grant that decides this belongs to the helper, since the helper is the
    process that runs `screencapture_region`. Preflighting the calling process
    instead would report the grant it inherited from whatever started it, which
    for a terminal session started over SSH is none -- a permanent false alarm on a
    host whose helper is perfectly authorized.

    A helper that never answers gets its own state rather than being folded into
    "no permission": the grant was not read, so claiming it is missing would be
    a guess, and the fix is a launchd one, not a System Settings one.
    """
    deadline = time.monotonic() + settle_s
    while True:
        try:
            result = ping(sock_path=sock_path)
        except PermissionsHelperError as exc:
            if time.monotonic() < deadline:
                time.sleep(_PROBE_RETRY_DELAY_S)
                continue
            return ScreenCaptureStatus(
                state=ScreenCaptureState.HELPER_UNREACHABLE,
                diagnostic=(
                    f"The permissions helper did not answer on its socket ({exc}), so its "
                    "Screen Recording grant could not be read -- this is a helper "
                    "liveness problem, not a permission one. While it is down every "
                    "desktop action it performs fails: OS-level screenshots, clicks, "
                    "keystrokes, window geometry. Check its launchd job "
                    "(`launchctl list | grep com.ava.permissions-helper`) and "
                    "$AVA_HOME/logs/permissions-helper.log."
                ),
            )
        if result["preflight_screen"]:
            return ScreenCaptureStatus(state=ScreenCaptureState.AVAILABLE)
        return ScreenCaptureStatus(
            state=ScreenCaptureState.NO_GRANT, diagnostic=_NO_GRANT_DIAGNOSTIC
        )


def check_accessibility(
    *, sock_path: str | Path | None = None, settle_s: float = _PROBE_SETTLE_S
) -> AccessibilityStatus:
    """Report whether the helper holds the Accessibility grant.

    The helper posts the synthetic input and reads the accessibility tree, so a
    caller-side probe would report a different process's grant. An unreachable
    helper is distinct from a missing grant: its answer was never read and the
    repair is its launchd job, not System Settings.
    """
    deadline = time.monotonic() + settle_s
    while True:
        try:
            result = ping(sock_path=sock_path)
        except PermissionsHelperError as exc:
            if time.monotonic() < deadline:
                time.sleep(_PROBE_RETRY_DELAY_S)
                continue
            return AccessibilityStatus(
                state=AccessibilityState.HELPER_UNREACHABLE,
                diagnostic=(
                    f"The permissions helper did not answer on its socket ({exc}), so its "
                    "Accessibility grant could not be read -- this is a helper liveness "
                    "problem, not a permission one. While it is down every desktop action "
                    "it performs fails: OS-level screenshots, clicks, keystrokes, window "
                    "geometry. Check its launchd job "
                    "(`launchctl list | grep com.ava.permissions-helper`) and "
                    "$AVA_HOME/logs/permissions-helper.log."
                ),
            )
        # The Windows helper has no Accessibility concept: SendInput is not
        # TCC-gated, so its older ping shape correctly means this axis is granted.
        if "ax_trusted" not in result or result["ax_trusted"] is True:
            return AccessibilityStatus(state=AccessibilityState.GRANTED)
        return AccessibilityStatus(
            state=AccessibilityState.NOT_GRANTED, diagnostic=_NO_AX_GRANT_DIAGNOSTIC
        )
