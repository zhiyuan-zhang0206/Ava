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

from .client_stateful import StatefulCalls
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
    IndicatorStopResult as IndicatorStopResult,
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

LINE_LIMIT = (
    64 * 1024 * 1024
)  # client-side cap on one response line; metadata stays small but keep headroom
_CONNECT_ATTEMPTS = 5
CONNECT_DELAY_S = 0.2
CALL_TIMEOUT_S = 30.0  # bound a connected read so a stalled helper surfaces as an error, not a hang
# A helper launchd has only just bootstrapped may not have bound its socket yet.
# Keep retrying the preflight ping for this long so a cold start is not reported
# as a dead helper -- a false alarm is the exact failure this probe replaced.
_PROBE_SETTLE_S = 5.0
PROBE_RETRY_DELAY_S = 0.25  # a ping can also fail instantly, so pace the retry


class PermissionsHelperError(RuntimeError):
    """A permissions helper call failed, or the daemon was unreachable."""


class UserAbortError(PermissionsHelperError):
    """The helper refused or stopped a desktop action: the user parked the mouse at the
    main display's top-left corner. The action must not be retried or replayed."""


def connect(path: str) -> socket.socket:
    """Open the helper's Unix socket, retrying briefly while it is absent or refusing."""
    phase = ["socket"]
    policy = Policy(
        max_attempts=_CONNECT_ATTEMPTS,
        backoff=lambda attempt: CONNECT_DELAY_S,  # noqa: ARG005 — Backoff keyword name
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


def _exchange(s: socket.socket, method: str, req: dict[str, object]) -> Any:
    """Send one request line on a connected socket, read one reply, close it."""
    s.settimeout(CALL_TIMEOUT_S)
    try:
        s.sendall((json.dumps(req) + "\n").encode())
        buf = bytearray()
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
            if len(buf) > LINE_LIMIT:
                raise PermissionsHelperError("permissions helper response exceeded line limit")
    except TimeoutError as e:
        raise PermissionsHelperError(
            f"permissions helper did not respond to {method!r} within {CALL_TIMEOUT_S}s"
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
        # `error_code` is optional: only the helper's user-abort stop sets it.
        if resp.get("error_code") == "user_abort":
            raise UserAbortError(resp["error"])
        raise PermissionsHelperError(resp["error"])
    return resp["result"]


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


class PermissionsHelperClient(StatefulCalls):
    """The calls to one permissions helper, over the Unix socket it listens on.

    The socket is fixed at construction: the composition root that owns the helper's
    cluster (``base.sessions.helperproc.helper_client`` for the usual per-cluster helper)
    decides which helper a caller talks to, and every call below dials that one.
    """

    def __init__(self, sock_path: str | Path) -> None:
        self._sock_path = Path(sock_path)

    @property
    def sock_path(self) -> Path:
        return self._sock_path

    def call(self, method: str, **args: object) -> Any:
        """One JSON-line request/response over this helper's Unix socket."""
        req = {"id": 1, "method": method, **args}  # one request per fresh connection
        return _exchange(connect(str(self._sock_path)), method, req)

    def ping(self) -> PingResult:
        """Report the helper's liveness and whether it holds the desktop grants."""
        return self.call("ping")

    def screencapture_region(
        self, x: int, y: int, w: int, h: int, path: str
    ) -> ScreencaptureResult:
        """Capture the screen rectangle (x, y, w, h) to a PNG at `path`."""
        return self.call("screencapture_region", x=x, y=y, w=w, h=h, path=path)

    def list_dir(self, path: str) -> list[PermissionsFileEntry]:
        """List a whitelisted directory's immediate entries, sorted by name."""
        result: FileListResult = self.call("file_list", path=path)
        return result["entries"]

    def read_file(self, path: str) -> bytes:
        """Read a whitelisted regular file of at most 32 MiB."""
        result: FileReadResult = self.call("file_read", path=path)
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
        self,
        x: float,
        y: float,
        *,
        double: bool = False,
        button: str = "left",
        click_count: int | None = None,
        modifiers: list[str] | None = None,
        duration_ms: float | None = None,
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
            self._require_native_input()
        return self.call("click", x=x, y=y, double=double, **args)

    def _require_native_input(self) -> None:
        if self.ping().get("native_input_v1") is not True:
            raise PermissionsHelperError(
                "permissions helper lacks native_input_v1; update it before using extended input"
            )

    def move(self, x: float, y: float, *, modifiers: list[str] | None = None) -> MoveResult:
        """Move the pointer without pressing a button (logical global coordinates)."""
        return self.call("move", x=x, y=y, modifiers=[] if modifiers is None else modifiers)

    def cursor_position(self) -> ClickPoint:
        """Read the actual global pointer position in logical points."""
        return self.call("cursor_position")

    def require_capability(self, capability: str, use: str) -> None:
        """Refuse `use` on a running helper that does not advertise `capability`.

        A helper built before a feature would ignore its options or not know its methods, so the
        client says what is missing instead of letting the call half-work."""
        if self.ping().get(capability) is not True:
            raise PermissionsHelperError(
                f"permissions helper lacks {capability}; update it before using {use}"
            )

    def drag(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        *,
        button: str | None = None,
        modifiers: list[str] | None = None,
    ) -> DragResult:
        """Drag a mouse button (left unless `button` names another) between global screen points
        (logical coordinates), with `modifiers` held from before the press until after the release."""
        args: dict[str, object] = {}
        if button is not None:
            args["button"] = button
        if modifiers is not None:
            args["modifiers"] = modifiers
        if args:
            self.require_capability("mouse_hold_v1", "drag options")
        return self.call(
            "drag",
            start_x=start_x,
            start_y=start_y,
            end_x=end_x,
            end_y=end_y,
            **args,
        )

    def type_text(self, text: str) -> TypeResult:
        """Type `text` as keyboard input into the focused field (handles CJK)."""
        return self.call("type", text=text)

    def key(
        self,
        code: int,
        *,
        cmd: bool = False,
        modifiers: list[str] | None = None,
        duration_ms: float | None = None,
    ) -> KeyResult:
        """Press and release one virtual key, optionally holding it with modifiers."""
        args: dict[str, object] = {}
        if modifiers is not None:
            args["modifiers"] = modifiers
        if duration_ms is not None:
            args["duration_ms"] = duration_ms
        if args:
            self._require_native_input()
        return self.call("key", code=code, cmd=cmd, **args)

    def scroll(
        self,
        x: float,
        y: float,
        dy: int,
        *,
        dx: int | None = None,
        modifiers: list[str] | None = None,
    ) -> ScrollResult:
        """Scroll by signed vertical/horizontal pixel deltas at a logical global point."""
        args: dict[str, object] = {}
        if dx is not None:
            args["dx"] = dx
        if modifiers is not None:
            args["modifiers"] = modifiers
        if args:
            self._require_native_input()
        return self.call("scroll", x=x, y=y, dy=dy, **args)

    def _require_capture_indicator(self) -> None:
        if self.ping().get("capture_indicator_v1") is not True:
            raise PermissionsHelperError(
                "permissions helper lacks capture_indicator_v1; update it before using the capture indicator"
            )

    def indicator_stop(self) -> IndicatorStopResult:
        """End the capture badge now; a no-op when none is showing.

        The helper shows the badge itself, on the window each input lands on, and ends it after
        ten idle seconds; this is for handing the screen over sooner.
        """
        self._require_capture_indicator()
        return self.call("indicator_stop")

    def list_apps(self) -> AppsResult:
        """List running GUI applications by process and bundle identity."""
        return self.call("list_apps")

    def list_windows(self, app: str | None = None) -> WindowsResult:
        """List live window-server identities, optionally for one exact application."""
        args: dict[str, object] = {} if app is None else {"app": app}
        return self.call("list_windows", **args)

    def focus_app(self, target: dict[str, object]) -> FocusAppResult:
        """Explicitly activate a running app selected by pid or unique bundle identity."""
        return self.call("focus_app", **target)

    def screencapture_window(self, pid: int, window_id: int, path: str) -> WindowCaptureResult:
        """Capture the identified window, verifying its live owning process."""
        return self.call("screencapture_window", pid=pid, window_id=window_id, path=path)

    def ax_window_info(self, app: str) -> AxWindowInfo:
        """Report the on-screen geometry of `app`'s focused window via accessibility."""
        return self.call("ax_window_info", app=app)

    def ax_tree(
        self,
        app: str | None = None,
        *,
        target: dict[str, object] | None = None,
        scope: int | None = None,
        scope_fp: str | None = None,
        max_nodes: int = 600,
        max_depth: int = 14,
        budget_ms: int = 1500,
        timeout_ms: int = 400,
        enable_ax: bool = True,
    ) -> AxTreeResult:
        """Read the selected process's focused-window accessibility tree (or the subtree under the
        raw id `scope`, whose fingerprint is `scope_fp`), bounded by node, depth and
        time limits. `target` selects PID or unique bundle ID without activation;
        legacy `app` names must resolve unambiguously."""
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
        if target is not None:
            if app is not None:
                raise ValueError("ax_tree takes exactly one of app or target")
            req.update(target)
        elif app is not None:
            req["app"] = app
        return self.call("ax_tree", **req)

    def ax_act(
        self,
        app: str,
        raw_id: int,
        action: str,
        *,
        pid: int | None = None,
        value: str | None = None,
        native_action: str | None = None,
        text: str | None = None,
        prefix: str | None = None,
        suffix: str | None = None,
        selection_type: str | None = None,
        timeout_ms: int = 2000,
    ) -> AxActResult:
        """Perform `action` (press / show_menu / focus / set_value) on the element
        the latest walk numbered `raw_id`. `pid` pins the observed process, while
        `app` remains display metadata. `value` is written, never echoed."""
        req: dict[str, object] = {"timeout_ms": timeout_ms}
        if pid is not None:
            req["pid"] = pid
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
        return self.call("ax_act", app=app, id=raw_id, action=action, **req)

    def window_info(self, owner: str) -> WindowInfo:
        """Report the geometry of `owner`'s normal on-screen window via the window list."""
        return self.call("window_info", owner=owner)

    def session_info(self) -> SessionInfo:
        """Report whether the login session is locked or off-console."""
        return self.call("session_info")

    def spawn_process(
        self,
        name: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str,
        stdout: str,
        stderr: str,
    ) -> SpawnResult:
        """Spawn or reuse a named direct child of the permissions helper."""
        result: SpawnResult = self.call(
            "spawn",
            name=name,
            argv=argv,
            env=env,
            cwd=cwd,
            stdout=stdout,
            stderr=stderr,
        )
        return result

    def session_list(self, prefix: str = "") -> list[SessionProc]:
        """List helper-owned process sessions whose names start with `prefix`."""
        result: SessionListResult = self.call("session_list", prefix=prefix)
        return result["sessions"]

    def session_has(self, name: str) -> bool:
        """Report whether the named helper-owned process session is alive."""
        result: AliveResult = self.call("session_has", name=name)
        return result["alive"]

    def signal_session(
        self,
        *,
        name: str | None = None,
        pid: int | None = None,
        sig: int = 15,
    ) -> bool:
        """Send `sig` to exactly one named session or explicit pid."""
        if (name is None) == (pid is None):
            raise ValueError("exactly one of name or pid is required")
        if name is not None:
            result: SignalResult = self.call("signal", name=name, sig=sig)
        else:
            result = self.call("signal", pid=pid, sig=sig)
        return result["sent"]

    def seed_root(self, config: RootSeedConfig) -> RootStatus:
        """Tell the root keeper what ava-root to launch and keep alive.

        The seed never disrupts a live root it already started — it applies to the
        next spawn. A run dir already held by another live root leaves the keeper
        in `conflict`: nothing is spawned, nothing is killed.
        """
        result: RootStatus = self.call("root_seed", config=config)
        return result

    def root_status(self) -> RootStatus:
        """Report the root keeper's state (works even when no seed is configured)."""
        result: RootStatus = self.call("root_status")
        return result

    def stop_root(self) -> RootStatus:
        """Durably stop the owned root; foreign or unknown custody always refuses."""
        result: RootStatus = self.call("root_stop")
        return result

    def shutdown_helper(self, run_dir: Path) -> HelperShutdownResult:
        """Close this home's native admission, retain intent, and request exit zero."""
        return self.call("helper_shutdown", run_dir=str(run_dir))

    def screen_size(self) -> ScreenSize:
        """Report the main display's geometry in logical points + backing scale.

        Computer-use callers use this to map screenshot pixels (physical) to
        click coordinates (logical): divide by `scale`."""
        return self.call("screen_size")

    def frontmost_app(self) -> FrontmostApp:
        """Report the frontmost application's display name ("" when none)."""
        return self.call("frontmost_app")

    def check_screen_capture(self, *, settle_s: float = _PROBE_SETTLE_S) -> ScreenCaptureStatus:
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
                result = self.ping()
            except PermissionsHelperError as exc:
                if time.monotonic() < deadline:
                    time.sleep(PROBE_RETRY_DELAY_S)
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

    def check_accessibility(self, *, settle_s: float = _PROBE_SETTLE_S) -> AccessibilityStatus:
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
                result = self.ping()
            except PermissionsHelperError as exc:
                if time.monotonic() < deadline:
                    time.sleep(PROBE_RETRY_DELAY_S)
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
