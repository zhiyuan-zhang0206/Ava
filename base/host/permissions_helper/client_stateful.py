"""Permissions-helper calls whose effect outlives the call: the clipboard and a held mouse button.

Both change state the user can see after the request returns, so each refuses a helper that
does not advertise it (`require_capability`) and neither puts the content it carries into an
error: the clipboard text may be a secret, and a held button must be traceable but not narrated.
"""

from __future__ import annotations

from typing import Any

from .wire import (
    ClipboardReadResult,
    ClipboardWriteResult,
    MouseDownResult,
    MouseReleaseAllResult,
    MouseUpResult,
)


class StatefulCalls:
    """The helper calls whose effect outlives the call; `PermissionsHelperClient` inherits them.

    They sit apart only to keep the client module within the file budget: each one rides the
    client's own `call` and `require_capability`."""

    def call(self, method: str, **args: object) -> Any:
        raise NotImplementedError

    def require_capability(self, capability: str, use: str) -> None:
        raise NotImplementedError

    def mouse_down(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        button: str = "left",
        modifiers: list[str] | None = None,
    ) -> MouseDownResult:
        """Press a mouse button and keep it down across calls, at a logical global point (the
        pointer's own position when none is given). `modifiers` come down first and stay down until
        the release. At most one button is held; the helper lets it go by itself on a stop gesture,
        after 30 idle seconds, or on `mouse_release_all`."""
        self.require_capability(
            "mouse_hold_v1",
            "mouse_down/mouse_up",
        )
        args: dict[str, object] = {"button": button}
        if x is not None and y is not None:
            args.update(x=x, y=y)
        elif x is not None or y is not None:
            raise ValueError("mouse_down takes both x and y, or neither")
        if modifiers is not None:
            args["modifiers"] = modifiers
        return self.call("mouse_down", **args)

    def mouse_up(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        button: str = "left",
    ) -> MouseUpResult:
        """Release the button `mouse_down` pressed, at a logical global point (dragging there first)
        or where the pointer is. Refused with an error when that button is not held."""
        self.require_capability(
            "mouse_hold_v1",
            "mouse_down/mouse_up",
        )
        args: dict[str, object] = {"button": button}
        if x is not None and y is not None:
            args.update(x=x, y=y)
        elif x is not None or y is not None:
            raise ValueError("mouse_up takes both x and y, or neither")
        return self.call("mouse_up", **args)

    def mouse_release_all(self) -> MouseReleaseAllResult:
        """Release whatever button Ava holds, quietly and regardless of a stop gesture: the cleanup
        the daemon runs when the screen changes hands. Names the buttons it released."""
        self.require_capability(
            "mouse_hold_v1",
            "mouse_down/mouse_up",
        )
        return self.call(
            "mouse_release_all",
        )

    def clipboard_read(self) -> ClipboardReadResult:
        """Read the clipboard's plain text and the list of types it currently holds.

        `text` is None when no plain text is on the clipboard. The content is returned to the
        caller only; nothing in this layer logs it."""
        self.require_capability(
            "clipboard_v1",
            "the clipboard",
        )
        return self.call(
            "clipboard_read",
        )

    def clipboard_write(self, text: str) -> ClipboardWriteResult:
        """Replace the clipboard with plain text (`public.utf8-plain-text`) and nothing else.

        The reply carries the length only, so the text is never echoed into a log or an audit row."""
        self.require_capability(
            "clipboard_v1",
            "the clipboard",
        )
        return self.call(
            "clipboard_write",
            text=text,
        )
