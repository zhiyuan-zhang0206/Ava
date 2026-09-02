"""Read-only contract tests for the memory search reconciliation script."""

from __future__ import annotations

import io
import sys

import pytest

from scripts import memory_search_reconcile as reconcile


class _FakeProvider:
    dim = 8
    fingerprint = "test:provider:dim=8"

    def embed_query(self, text: str) -> object:
        del text
        return object()


class _FakeBackend:
    def __init__(self, connect_error: RuntimeError | None = None) -> None:
        self.connect_error = connect_error
        self.connected = False
        self.closed = False

    def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    def close(self) -> None:
        self.closed = True

    def all_meta(self) -> dict[str, tuple[float, str, str]]:
        return {}

    def search_topk(self, vector: object, k: int) -> list[str]:
        del vector, k
        return []


class _TTYStdin:
    def isatty(self) -> bool:
        return True


def _one_query(limit: int) -> list[str]:
    return [f"query-{limit}"]


def _confirmation_with_space(_prompt: str = "") -> str:
    return "yes "


def _exact_confirmation(_prompt: str = "") -> str:
    return "yes"


def _patch_reconcile_dependencies(
    monkeypatch: pytest.MonkeyPatch, backends: list[_FakeBackend]
) -> list[tuple[str, int, str, bool]]:
    calls: list[tuple[str, int, str, bool]] = []
    monkeypatch.setattr(reconcile, "_sample_queries", _one_query)
    monkeypatch.setattr(reconcile, "get_provider", _FakeProvider)

    def _get_backend_named(
        name: str, *, dim: int, fingerprint: str, readonly: bool
    ) -> _FakeBackend:
        calls.append((name, dim, fingerprint, readonly))
        return backends.pop(0)

    monkeypatch.setattr(reconcile, "get_backend_named", _get_backend_named)
    return calls


def test_default_run_connects_both_backends_readonly(monkeypatch: pytest.MonkeyPatch) -> None:
    """The normal comparison path cannot obtain a write-capable backend."""
    backends = [_FakeBackend(), _FakeBackend()]
    calls = _patch_reconcile_dependencies(monkeypatch, backends)
    monkeypatch.setattr(sys, "argv", ["memory_search_reconcile", "--a", "milvus", "--b", "numpy"])

    assert reconcile.main() == 0
    assert calls == [
        ("milvus", 8, "test:provider:dim=8", True),
        ("numpy", 8, "test:provider:dim=8", True),
    ]


def test_allow_write_without_exact_confirmation_never_connects_writable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag is only the first confirmation; any other answer aborts."""
    backends = [_FakeBackend(), _FakeBackend()]
    calls = _patch_reconcile_dependencies(monkeypatch, backends)
    monkeypatch.setattr(
        sys, "argv", ["memory_search_reconcile", "--a", "milvus", "--b", "numpy", "--allow-write"]
    )
    monkeypatch.setattr(reconcile.sys, "stdin", _TTYStdin())
    monkeypatch.setattr(reconcile.builtins, "input", _confirmation_with_space)

    assert reconcile.main() == 1
    assert calls == []


def test_allow_write_requires_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Piped input cannot supply the second confirmation."""
    backends = [_FakeBackend(), _FakeBackend()]
    calls = _patch_reconcile_dependencies(monkeypatch, backends)
    monkeypatch.setattr(
        sys,
        "argv",
        ["memory_search_reconcile", "--a", "milvus", "--b", "numpy", "--allow-write"],
    )
    monkeypatch.setattr(reconcile.sys, "stdin", io.StringIO("yes\n"))

    assert reconcile.main() == 1
    assert calls == []


def test_allow_write_with_exact_confirmation_connects_writable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator who types exact yes receives explicitly writable backends."""
    backends = [_FakeBackend(), _FakeBackend()]
    calls = _patch_reconcile_dependencies(monkeypatch, backends)
    monkeypatch.setattr(
        sys, "argv", ["memory_search_reconcile", "--a", "milvus", "--b", "numpy", "--allow-write"]
    )
    monkeypatch.setattr(reconcile.sys, "stdin", _TTYStdin())
    monkeypatch.setattr(reconcile.builtins, "input", _exact_confirmation)

    assert reconcile.main() == 0
    assert [call[-1] for call in calls] == [False, False]


def test_readonly_connection_mismatch_is_reported_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Storage validation failures tell the operator who owns repair."""
    backends = [_FakeBackend(RuntimeError("Milvus collection schema mismatch")), _FakeBackend()]
    _patch_reconcile_dependencies(monkeypatch, backends)
    monkeypatch.setattr(sys, "argv", ["memory_search_reconcile", "--a", "milvus", "--b", "numpy"])

    assert reconcile.main() == 1

    err = capsys.readouterr().err
    assert "schema mismatch" in err
    assert "indexer daemon's cold-start reconcile" in err
    assert "Traceback" not in err
