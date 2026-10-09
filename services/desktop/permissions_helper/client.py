"""Client for the macOS permissions helper daemon.

Connects to this cluster's helper and exchanges one line-delimited JSON
request/response per call over a Unix socket. Skills that drive the macOS
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
import json
import socket
import time
from pathlib import Path
from typing import Any

from base.host.converge.accessibility import AccessibilityState, AccessibilityStatus
from base.host.converge.screen_capture import ScreenCaptureState, ScreenCaptureStatus
from base.host.net.resilience import Policy, retry
from base.paths import permissions_helper_socket

from .wire import (
    AliveResult as AliveResult,
)
from .wire import (
    AppsResult as AppsResult,
)
from .wire import (
    AxActResult as AxActResult,
)
from .wire import (
    AxNode as AxNode,
)
from .wire import (
    AxTreeResult as AxTreeResult,
)
from .wire import (
    AxWindowInfo as AxWindowInfo,
)
from .wire import (
    ClickPoint as ClickPoint,
)
from .wire import (
    ClickResult as ClickResult,
)
from .wire import (
    DragResult as DragResult,
)
from .wire import (
    FileListResult as FileListResult,
)
from .wire import (
    FileReadResult as FileReadResult,
)
from .wire import (
    FocusAppResult as FocusAppResult,
)
from .wire import (
    FrontmostApp as FrontmostApp,
)
from .wire import (
    HelperShutdownResult as HelperShutdownResult,
)
from .wire import (
    KeyResult as KeyResult,
)
from .wire import (
    MoveResult as MoveResult,
)
from .wire import (
    PermissionsFileEntry as PermissionsFileEntry,
)
from .wire import (
    PingResult as PingResult,
)
from .wire import (
    RootConflictInfo as RootConflictInfo,
)
from .wire import (
    RootExitInfo as RootExitInfo,
)
from .wire import (
    RootSeedConfig as RootSeedConfig,
)
from .wire import (
    RootSeedReport as RootSeedReport,
)
from .wire import (
    RootStatus as RootStatus,
)
from .wire import (
    ScreencaptureResult as ScreencaptureResult,
)
from .wire import (
    ScreenSize as ScreenSize,
)
from .wire import (
    ScrollResult as ScrollResult,
)
from .wire import (
    SessionInfo as SessionInfo,
)
from .wire import (
    SessionListResult as SessionListResult,
)
from .wire import (
    SessionProc as SessionProc,
)
from .wire import (
    SignalResult as SignalResult,
)
from .wire import (
    SpawnResult as SpawnResult,
)
from .wire import (
    TypeResult as TypeResult,
)
from .wire import (
    WindowCaptureResult as WindowCaptureResult,
)
from .wire import (
    WindowGeometry as WindowGeometry,
)
from .wire import (
    WindowInfo as WindowInfo,
)
from .wire import (
    WindowsResult as WindowsResult,
)

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


class PermissionsHelperError(RuntimeError):
    """A permissions helper call failed, or the daemon was unreachable."""


def connect(path: str) -> socket.socket:
    """Open the helper's Unix socket, retrying briefly while it is absent or refusing."""
    phase = ["socket"]
    policy = Policy(
        max_attempts=_CONNECT_ATTEMPTS,
        backoff=lambda attempt: _CONNECT_DELAY_S,  # noqa: ARG005 — Backoff keyword name
        jitter="none",
        jitter_span=1.0,
        classify=lambda exc: (
            phase[0] == "connect" and isinstance(exc, (FileNotFoundError, ConnectionRefusedError))
        ),
        idempotent=True,
        respect_retry_after=False,
        on_final_failure=None,
    )

    def once() -> socket.socket:
        phase[0] = "socket"
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        phase[0] = "connect"
        try:
            s.connect(path)
            return s
        except PermissionError:
            # Not an unreachability signal — the socket exists and answers, but
            # this caller's ACLs are wrong. Surface it raw instead of closing
            # the socket and relabeling it "not reachable": that message would
            # send an operator chasing a dead helper instead of a permissions fix.
            raise
        except OSError:
            phase[0] = "close"
            s.close()
            phase[0] = "connect"
            raise

    last: OSError | None = None
    try:
        return retry(policy)(once)
    except PermissionError:
        raise
    except OSError as exc:
        if phase[0] != "connect":
            raise
        last = exc
    raise PermissionsHelperError(f"permissions helper not reachable at {path}: {last}")


def _call(
    method: str,
    *,
    sock_path: str | Path | None = None,
    **args: object,
) -> Any:
    """One JSON-line request/response over this cluster's Unix socket."""
    req = {"id": 1, "method": method, **args}  # one request per fresh connection
    path = str(sock_path or permissions_helper_socket())
    return _exchange(connect(path), method, req)


def _exchange(s: socket.socket, method: str, req: dict[str, object]) -> Any:
    """Send one request line on a connected socket, read one reply, close it."""
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
    return parse_reply(bytes(buf), method)


def parse_reply(buf: bytes, method: str) -> Any:
    """The helper's JSON-line reply contract."""
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
    x: float,
    y: float,
    *,
    double: bool = False,
    button: str = "left",
    click_count: int | None = None,
    modifiers: list[str] | None = None,
    duration_ms: float | None = None,
    sock_path: str | Path | None = None,
) -> ClickResult:
    """Click at a logical global screen point, optionally with modifiers and a hold."""
    args: dict[str, object] = {}
    if button != "left":
        args["button"] = button
    if click_count is not None:
        args["click_count"] = click_count
    if modifiers is not None:
        args["modifiers"] = modifiers
    if duration_ms is not None:
        args["duration_ms"] = duration_ms
    if args:
        _require_native_input(sock_path)
    return _call("click", x=x, y=y, double=double, sock_path=sock_path, **args)


def _require_native_input(sock_path: str | Path | None) -> None:
    if ping(sock_path=sock_path).get("native_input_v1") is not True:
        raise PermissionsHelperError(
            "permissions helper lacks native_input_v1; update it before using extended input"
        )


def move(
    x: float, y: float, *, modifiers: list[str] | None = None, sock_path: str | Path | None = None
) -> MoveResult:
    """Move the pointer without pressing a button (logical global coordinates)."""
    return _call(
        "move", x=x, y=y, modifiers=[] if modifiers is None else modifiers, sock_path=sock_path
    )


def cursor_position(*, sock_path: str | Path | None = None) -> ClickPoint:
    """Read the actual global pointer position in logical points."""
    return _call("cursor_position", sock_path=sock_path)


def drag(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    *,
    sock_path: str | Path | None = None,
) -> DragResult:
    """Drag the left mouse button between global screen points (logical coordinates)."""
    return _call(
        "drag", start_x=start_x, start_y=start_y, end_x=end_x, end_y=end_y, sock_path=sock_path
    )


def type_text(text: str, *, sock_path: str | Path | None = None) -> TypeResult:
    """Type `text` as keyboard input into the focused field (handles CJK)."""
    return _call("type", text=text, sock_path=sock_path)


def key(
    code: int,
    *,
    cmd: bool = False,
    modifiers: list[str] | None = None,
    duration_ms: float | None = None,
    sock_path: str | Path | None = None,
) -> KeyResult:
    """Press and release one virtual key, optionally holding it with modifiers."""
    args: dict[str, object] = {}
    if modifiers is not None:
        args["modifiers"] = modifiers
    if duration_ms is not None:
        args["duration_ms"] = duration_ms
    if args:
        _require_native_input(sock_path)
    return _call("key", code=code, cmd=cmd, sock_path=sock_path, **args)


def scroll(
    x: float,
    y: float,
    dy: int,
    *,
    dx: int | None = None,
    modifiers: list[str] | None = None,
    sock_path: str | Path | None = None,
) -> ScrollResult:
    """Scroll by signed vertical/horizontal pixel deltas at a logical global point."""
    args: dict[str, object] = {}
    if dx is not None:
        args["dx"] = dx
    if modifiers is not None:
        args["modifiers"] = modifiers
    if args:
        _require_native_input(sock_path)
    return _call("scroll", x=x, y=y, dy=dy, sock_path=sock_path, **args)


def list_apps(*, sock_path: str | Path | None = None) -> AppsResult:
    """List running GUI applications by process and bundle identity."""
    return _call("list_apps", sock_path=sock_path)


def list_windows(app: str | None = None, *, sock_path: str | Path | None = None) -> WindowsResult:
    """List live window-server identities, optionally for one exact application."""
    args: dict[str, object] = {} if app is None else {"app": app}
    return _call("list_windows", sock_path=sock_path, **args)


def focus_app(target: dict[str, object], *, sock_path: str | Path | None = None) -> FocusAppResult:
    """Explicitly activate a running app selected by pid or unique bundle identity."""
    return _call("focus_app", sock_path=sock_path, **target)


def screencapture_window(
    pid: int, window_id: int, path: str, *, sock_path: str | Path | None = None
) -> WindowCaptureResult:
    """Capture the identified window, verifying its live owning process."""
    return _call(
        "screencapture_window", pid=pid, window_id=window_id, path=path, sock_path=sock_path
    )


def ax_window_info(app: str, *, sock_path: str | Path | None = None) -> AxWindowInfo:
    """Report the on-screen geometry of `app`'s focused window via accessibility."""
    return _call("ax_window_info", app=app, sock_path=sock_path)


def ax_tree(
    app: str,
    *,
    scope: int | None = None,
    scope_fp: str | None = None,
    max_nodes: int = 600,
    max_depth: int = 14,
    budget_ms: int = 1500,
    timeout_ms: int = 400,
    enable_ax: bool = True,
    sock_path: str | Path | None = None,
) -> AxTreeResult:
    """Read `app`'s focused-window accessibility tree (or the subtree under the
    raw id `scope`, whose fingerprint is `scope_fp`), bounded by node, depth and
    time limits."""
    req: dict[str, object] = {
        "max_nodes": max_nodes,
        "max_depth": max_depth,
        "budget_ms": budget_ms,
        "timeout_ms": timeout_ms,
        "enable_ax": enable_ax,
    }
    if scope is not None:
        req["scope"] = scope
        req["scope_fp"] = scope_fp
    return _call("ax_tree", app=app, sock_path=sock_path, **req)


def ax_act(
    app: str,
    raw_id: int,
    action: str,
    *,
    value: str | None = None,
    native_action: str | None = None,
    text: str | None = None,
    prefix: str | None = None,
    suffix: str | None = None,
    selection_type: str | None = None,
    timeout_ms: int = 2000,
    sock_path: str | Path | None = None,
) -> AxActResult:
    """Perform `action` (press / show_menu / focus / set_value) on the element
    the latest walk numbered `raw_id`. `value` is written, never echoed."""
    req: dict[str, object] = {"timeout_ms": timeout_ms}
    if value is not None:
        req["value"] = value
    for name, supplied in (
        ("native_action", native_action),
        ("text", text),
        ("prefix", prefix),
        ("suffix", suffix),
        ("selection_type", selection_type),
    ):
        if supplied is not None:
            req[name] = supplied
    return _call("ax_act", app=app, id=raw_id, action=action, sock_path=sock_path, **req)


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


def seed_root(config: RootSeedConfig, *, sock_path: str | Path | None = None) -> RootStatus:
    """Tell the root keeper what ava-root to launch and keep alive.

    The seed never disrupts a live root it already started — it applies to the
    next spawn. A run dir already held by another live root leaves the keeper
    in `conflict`: nothing is spawned, nothing is killed.
    """
    result: RootStatus = _call("root_seed", config=config, sock_path=sock_path)
    return result


def root_status(*, sock_path: str | Path | None = None) -> RootStatus:
    """Report the root keeper's state (works even when no seed is configured)."""
    result: RootStatus = _call("root_status", sock_path=sock_path)
    return result


def stop_root(*, sock_path: str | Path | None = None) -> RootStatus:
    """Durably stop the owned root; foreign or unknown custody always refuses."""
    result: RootStatus = _call("root_stop", sock_path=sock_path)
    return result


def shutdown_helper(run_dir: Path, *, sock_path: str | Path) -> HelperShutdownResult:
    """Close this home's native admission, retain intent, and request exit zero."""
    return _call("helper_shutdown", run_dir=str(run_dir), sock_path=sock_path)


def screen_size(*, sock_path: str | Path | None = None) -> ScreenSize:
    """Report the main display's geometry in logical points + backing scale.

    Computer-use callers use this to map screenshot pixels (physical) to
    click coordinates (logical): divide by `scale`."""
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
    # quiesce-exempt: a bounded permission-grant wait; no database
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
    # quiesce-exempt: a bounded permission-grant wait; no database
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
        if result["ax_trusted"] is True:
            return AccessibilityStatus(state=AccessibilityState.GRANTED)
        return AccessibilityStatus(
            state=AccessibilityState.NOT_GRANTED, diagnostic=_NO_AX_GRANT_DIAGNOSTIC
        )
