"""Regression coverage for batched session timestamps in host status."""

import pytest

from ops import cluster_status


class _BatchOnlyBackend:
    def __init__(self, name: str):
        self.name = name
        self.batch_calls: list[list[str]] = []

    def list_sessions(self, prefix: str = "") -> list[str]:
        return [self.name] if self.name.startswith(prefix) else []

    def session_started_at(self, name: str) -> float | None:
        raise AssertionError(f"single timestamp read used for {name}")

    def session_started_ats(self, names: list[str]) -> dict[str, float | None]:
        self.batch_calls.append(names)
        return dict.fromkeys(names, 1000.0)


def test_collect_sessions_batches_timestamp_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each backend receives one timestamp batch, never one read per session."""
    service = _BatchOnlyBackend("ava-main-restarter")
    shell = _BatchOnlyBackend("ava-main-agent-7-shell-0")
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: service)
    monkeypatch.setattr("shared.session_backend.get_shell_backend", lambda: shell)

    sessions, _, _ = cluster_status._collect_sessions()

    assert [session.name for session in sessions] == sorted([service.name, shell.name])
    assert service.batch_calls == [[service.name]]
    assert shell.batch_calls == [[shell.name]]
