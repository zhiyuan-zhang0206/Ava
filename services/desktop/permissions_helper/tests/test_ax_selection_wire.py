"""AX action requests cross the real client socket without touching a desktop.

The server below substitutes the helper transport endpoint, not macOS AX APIs.
Native matching and action execution are verified by the helper owner's harness.
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from services.desktop.permissions_helper import client


def roundtrip(kwargs: dict[str, Any], response: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    requests: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="ava-ax-wire-", dir="/tmp") as directory:
        path = Path(directory) / "helper.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            listener.listen(1)
            listener.settimeout(3)

            def serve() -> None:
                with listener.accept()[0] as connection, connection.makefile("rb") as stream:
                    requests.append(json.loads(stream.readline()))
                    connection.sendall(json.dumps(response).encode() + b"\n")

            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            try:
                result = client.ax_act("Mail", 7, sock_path=path, **kwargs)
            finally:
                worker.join(timeout=3)
                assert not worker.is_alive(), "test helper server did not finish"
    assert len(requests) == 1
    return requests[0], result


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"action": "press"}, {}),
        (
            {"action": "perform_action", "native_action": "AXCustomApplicationAction"},
            {"native_action": "AXCustomApplicationAction"},
        ),
        (
            {
                "action": "select_text",
                "text": "\U0001f600needle",
                "prefix": "",
                "suffix": "!",
                "selection_type": "cursor_after",
            },
            {
                "text": "\U0001f600needle",
                "prefix": "",
                "suffix": "!",
                "selection_type": "cursor_after",
            },
        ),
        ({"action": "select_text", "text": "needle"}, {"text": "needle"}),
    ],
)
def test_ax_extended_request_roundtrip_omits_absent_fields(
    kwargs: dict[str, Any], expected: dict[str, Any]
) -> None:
    request, result = roundtrip(kwargs, {"ok": True, "result": {"completed": True}})
    assert request == {
        "method": "ax_act",
        "app": "Mail",
        "id": 7,
        "action": kwargs["action"],
        "timeout_ms": 2000,
        **expected,
    }
    assert result == {"completed": True}
    assert "text" not in result and "prefix" not in result and "suffix" not in result


def test_ax_extended_helper_failure_surfaces_without_echoing_selection_payload() -> None:
    with pytest.raises(client.PermissionsHelperError, match="text selection is ambiguous") as error:
        roundtrip(
            {"action": "select_text", "text": "private-needle", "prefix": "private-context"},
            {"ok": False, "error": "text selection is ambiguous; add prefix or suffix"},
        )
    assert "private-needle" not in str(error.value) and "private-context" not in str(error.value)
