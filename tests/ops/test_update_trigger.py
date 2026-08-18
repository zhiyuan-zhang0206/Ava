"""Shared update-trigger behavior — ported from the watchdog daemon's
`TestSpawnUpdateHttp` when `_spawn_update` moved to `ops.controllers.update_trigger`.

`trigger_update()` must POST a `cluster_update` op to THIS host's own ops server
over loopback (never the gateway), arm the shared cooldown, and never raise on
an HTTP error.
"""

from __future__ import annotations

import time

import pytest

from ops.controllers import update_trigger as ut


@pytest.fixture(autouse=True)
def _reset_cooldown() -> None:
    ut.reset_cooldown()


class _FakeResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, object]:
        return {"status": "completed", "result": {"session": "ava-updater"}}


def test_posts_cluster_update_op_to_local_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    """The trigger dials the loopback ops server — no gateway, no target param."""
    called: dict[str, object] = {}

    def fake_post(url: str, **kw: object) -> _FakeResponse:
        called["url"] = url
        called["json"] = kw.get("json")
        called["headers"] = kw.get("headers")
        return _FakeResponse()

    monkeypatch.setattr(ut, "dial_post", fake_post)
    monkeypatch.setattr(ut, "health_port", lambda _name: 8106)  # pyright: ignore[reportUnknownArgumentType]
    assert ut.trigger_update() is True
    assert called["url"] == "http://127.0.0.1:8106/ops"
    # A watchdog self-heal always drains this host's agents first (mode=smooth).
    assert called["json"] == {"kind": "cluster_update", "payload": {"mode": "smooth"}}
    # Auth: bearer cluster secret when one is configured (tests run secret-less,
    # so the header is skipped there — same rule as dispatch_to_machine).
    assert isinstance(called["headers"], dict)


def test_passes_target_sha_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """target_sha (the cluster pin) rides in the op payload, not a query param."""
    called: dict[str, object] = {}

    def fake_post(url: str, **kw: object) -> _FakeResponse:
        called["json"] = kw.get("json")
        return _FakeResponse()

    monkeypatch.setattr(ut, "dial_post", fake_post)
    monkeypatch.setattr(ut, "health_port", lambda _name: 8106)  # pyright: ignore[reportUnknownArgumentType]
    ut.trigger_update(target_sha="abc1234")
    assert called["json"] == {
        "kind": "cluster_update",
        "payload": {"target_sha": "abc1234", "mode": "smooth"},
    }


def test_logs_warning_on_http_error_and_returns_false(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """HTTP errors are logged as warnings, not raised, and the trigger returns False."""

    def fake_post(*a: object, **kw: object) -> None:
        raise ut.httpx.ConnectError("refused")

    monkeypatch.setattr(ut, "dial_post", fake_post)
    monkeypatch.setattr(ut, "health_port", lambda _name: 8106)  # pyright: ignore[reportUnknownArgumentType]
    with caplog.at_level("WARNING"):
        assert ut.trigger_update() is False
    assert any("failed to trigger update" in r.message for r in caplog.records)


def test_returns_false_when_ops_rejects_op(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A reached ops server that reports status=failed is a failed trigger, not a success."""

    class _FailedResponse(_FakeResponse):
        def json(self) -> dict[str, object]:
            return {"status": "failed", "result": {"error": "updater already in flight"}}

    monkeypatch.setattr(ut, "dial_post", lambda *_a, **_kw: _FailedResponse())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(ut, "health_port", lambda _name: 8106)  # pyright: ignore[reportUnknownArgumentType]
    with caplog.at_level("WARNING"):
        assert ut.trigger_update() is False
    assert any("rejected cluster_update" in r.message for r in caplog.records)


def test_arms_cooldown_on_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cooldown is armed on the *attempt* (before the POST), so a failed POST
    still rate-limits the next tick."""

    def fake_post(*a: object, **kw: object) -> None:
        raise ut.httpx.ConnectError("refused")

    monkeypatch.setattr(ut, "dial_post", fake_post)
    monkeypatch.setattr(ut, "health_port", lambda _name: 8106)  # pyright: ignore[reportUnknownArgumentType]
    assert ut.in_cooldown() is False
    ut.trigger_update()
    assert ut.in_cooldown() is True


def test_in_cooldown_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """in_cooldown is False once UPDATE_COOLDOWN_S has elapsed since the last arm."""
    ut._last_update_spawn = time.monotonic() - (ut.UPDATE_COOLDOWN_S + 1)
    assert ut.in_cooldown() is False
