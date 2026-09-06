"""`services.healthchecks.memory_search` unit tests — probe seam + restart shape.

The healthcheck probes with a real POST /search (the store path the
gateway/indexer dial), not /healthz — a port-open probe stays green while
the service behind it is unusable. These tests pin that the probe wraps
the search answer into a DaemonProbe verdict (alive/down) and that the
restart path reports the probe's verdict rather than the spawn's — the
healthcheck runs the shared keepalive body (`run_keepalive`), which is
covered by `tests/services/test_healthcheck_gateway.py`-style tests of
`shared.service_respawn`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.healthchecks import memory_search as hc
from shared.daemon_health import DaemonProbe


def test_probe_up_when_search_answers_with_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc, "_post_search", lambda _uri: {"paths": []})  # pyright: ignore[reportUnknownArgumentType]
    result = hc._probe()
    assert result.alive is True
    assert result.terminal is False


def test_probe_down_when_search_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        hc,
        "_post_search",
        lambda _uri: DaemonProbe.down("connection refused"),  # pyright: ignore[reportUnknownArgumentType]
    )
    result = hc._probe()
    assert result.alive is False
    assert "connection refused" in result.detail


def test_probe_down_when_payload_lacks_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """A foreign process answering 200 on the port is not the search service."""
    monkeypatch.setattr(hc, "_post_search", lambda _uri: {"nope": 1})  # pyright: ignore[reportUnknownArgumentType]
    assert hc._probe().alive is False


def test_restart_respawns_session_and_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    """_restart_daemon respawns `memory-search` and returns the probe's verdict."""
    calls: list[tuple[str, str, Path]] = []

    def fake_respawn_and_verify(session, cmd, repo, *, verify, **_kw) -> DaemonProbe:
        calls.append((session, cmd, repo))  # pyright: ignore[reportUnknownArgumentType]
        return verify()

    monkeypatch.setattr(hc, "respawn_and_verify", fake_respawn_and_verify)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "_post_search", lambda _uri: {"paths": []})  # pyright: ignore[reportUnknownArgumentType]

    result = hc._restart_daemon()
    assert result.alive is True
    assert [(s, c) for s, c, _r in calls] == [
        ("memory-search", ".venv/bin/python -m services.memory_search.daemon")
    ]


def test_restart_reports_failure_when_daemon_never_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spawn accepted the command but the daemon never came up → NOT a success."""
    monkeypatch.setattr(
        hc,
        "respawn_and_verify",
        lambda *_a, verify, **_kw: verify(),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        hc,
        "_post_search",
        lambda _uri: DaemonProbe.down("POST /search failed"),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert hc._restart_daemon().alive is False


def test_main_runs_the_shared_keepalive(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() is exactly the shared keepalive body with this module's probe
    and respawn (the 2026-08-29 decision's one policy) — never a
    hand-rolled probe -> respawn -> exit loop."""
    seen: dict[str, object] = {}

    def fake_keepalive(label: str, log: object, *, probe: object, respawn: object) -> None:
        seen.update(label=label, probe=probe, respawn=respawn)

    monkeypatch.setattr(hc, "run_keepalive", fake_keepalive)
    monkeypatch.setattr(hc, "init_gateway_process", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]

    hc.main()
    assert seen["label"] == "memory_search"
    assert seen["probe"] is hc._probe
    assert seen["respawn"] is hc._restart_daemon
