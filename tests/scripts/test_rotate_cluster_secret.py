"""Control-plane bearer rotation never mutates data-plane credentials."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from scripts import rotate_cluster_secret as rotate
from shared.config import settings

_OLD = "old"
_NEW = "new"


def test_mint_secret_is_url_safe() -> None:
    assert set(rotate.mint_secret()) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    )


def test_build_state_refuses_no_auth_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    with pytest.raises(RuntimeError, match="no-auth"):
        rotate.build_state()


def test_preflight_checks_bootstrap_bearer_and_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    class _Response:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    def _get(url: str, *, headers: dict[str, str], timeout: float) -> _Response:
        assert timeout == 5.0
        calls.append((url, headers))
        return _Response(200 if headers["Authorization"] == f"Bearer {_OLD}" else 401)

    monkeypatch.setattr(rotate.httpx, "get", _get)
    state = rotate.RotationState(old_secret=_OLD, new_secret=_NEW, gateway_url="http://gateway")

    assert rotate.preflight(state) is True
    assert calls[0] == (
        "http://gateway/api/bootstrap",
        {"Authorization": f"Bearer {_OLD}"},
    )
    assert calls[1][0] == "http://gateway/api/bootstrap"
    assert calls[1][1]["Authorization"] != f"Bearer {_OLD}"


def test_write_env_changes_only_the_bearer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    writes: list[tuple[Path, dict[str, str]]] = []

    def _upsert(path: Path, values: dict[str, str], *, audit_site: str | None = None) -> None:
        writes.append((path, values))

    monkeypatch.setattr(rotate, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(rotate, "upsert_env", _upsert)
    state = rotate.RotationState(old_secret=_OLD, new_secret=_NEW, gateway_url="http://gateway")

    rotate.write_env(state)

    assert writes == [(tmp_path / ".env", {"AVA_CLUSTER_SECRET": _NEW})]


def test_recovery_state_is_owner_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(rotate, "ava_home", lambda: tmp_path)
    state = rotate.RotationState(old_secret=_OLD, new_secret=_NEW, gateway_url="http://gateway")

    path = state.save()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert rotate.RotationState.load(path) == state


def test_execute_stages_bearer_without_data_plane_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    state = rotate.RotationState(old_secret=_OLD, new_secret=_NEW, gateway_url="http://gateway")
    writes: list[rotate.RotationState] = []
    monkeypatch.setattr(rotate, "build_state", lambda: state)
    monkeypatch.setattr(rotate, "preflight", lambda _state: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(rotate, "print_plan", lambda *_args, **_kwargs: None)  # pyright: ignore[reportUnknownArgumentType]

    def _write(actual: rotate.RotationState) -> None:
        writes.append(actual)

    def _save(_state: rotate.RotationState) -> Path:
        return Path("bearer-state")

    monkeypatch.setattr(rotate, "write_env", _write)
    monkeypatch.setattr(rotate.RotationState, "save", _save)

    assert rotate.main(["--execute", "--yes"]) == 0
    assert writes == [state]
