"""Unit tests for shared.sessions.pty.host's request-parsing contract.

Split out of test_pty_sessions_cli.py (real detached-host/CLI end-to-end
tests) to stay under the structure-lint's per-file line budget: this needs
only the pure `_parse_request` function, no real session infrastructure.
"""

from __future__ import annotations

import pytest

from shared.sessions.pty.host import _parse_request


def test_parse_request_rejects_non_object() -> None:
    """A valid-JSON non-object request (list / string / op-less dict) must
    raise so the host answers `bad request` instead of dying silently."""
    with pytest.raises(TypeError):
        _parse_request(b'["kill", "x"]')
    with pytest.raises(TypeError):
        _parse_request(b'"ping"')
    with pytest.raises(TypeError):
        _parse_request(b'{"noop": 1}')
    req = _parse_request(b'{"op": "ping"}')
    assert req["op"] == "ping"
