"""Destroy closes native ownership before marking a home detached."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from base import cluster
from cli.commands.cluster import home as lifecycle


def _detached(home: Path) -> bool:
    marker = home / "destroy-intent.json"
    return marker.exists() and '"detached"' in marker.read_text()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A born worktree gateway home, bound to its checkout."""
    from cli.start_identity import IdentityInput, prepare_identity

    home = tmp_path / "home"
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    def port_free(_port: int) -> bool:
        return True

    monkeypatch.setattr(cluster, "port_free", port_free)
    prepare_identity(IdentityInput(home, checkout, True, frozenset({"gateway"}), {}))

    def down(**_kw: object) -> int:
        return 0

    def unregister(_home: Path) -> None:
        return None

    def helper(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(lifecycle, "cmd_cluster_down", down)
    monkeypatch.setattr(lifecycle, "_unregister_scheduled_jobs", unregister)
    monkeypatch.setattr("services.permissions_helper.launchd_job.unregister_helper", helper)
    return home


def test_destroy_checks_helper_before_detaching(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.permissions_helper import launchd_job

    seen: list[Path] = []

    def unregister(target: Path, *, helper_port: int) -> None:
        rec = cluster.get_record(home)
        assert rec is not None
        assert (home / "destroy-intent.json").exists()
        assert helper_port == rec.ports["permissions_helper"]
        seen.append(target)

    monkeypatch.setattr(launchd_job, "unregister_helper", unregister)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 0
    assert seen == [home]
    assert _detached(home)


def test_stop_failure_leaves_home_attached_and_never_unloads_helper(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed_stop(**_kw: object) -> int:
        return 4

    monkeypatch.setattr(lifecycle, "cmd_cluster_down", failed_stop)
    unload = Mock()
    monkeypatch.setattr("services.permissions_helper.launchd_job.unregister_helper", unload)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 4
    assert not _detached(home)
    unload.assert_not_called()


@pytest.mark.parametrize("stage", ["jobs", "helper"])
def test_ambiguous_cleanup_leaves_home_attached(
    home: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("ownership unknown")

    if stage == "jobs":
        monkeypatch.setattr(lifecycle, "_unregister_scheduled_jobs", fail)
    else:
        monkeypatch.setattr("services.permissions_helper.launchd_job.unregister_helper", fail)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 1
    assert not _detached(home)


def test_destroy_preserves_credentials_and_data(home: Path) -> None:
    env = home / ".env"
    env.write_text("PRIVATE_KEY=retain\n")
    data = home / "pg" / "data"
    data.mkdir(parents=True)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 0
    assert env.read_text() == "PRIVATE_KEY=retain\n"
    assert data.is_dir()


def test_destroy_refuses_default_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def default(_home: Path) -> bool:
        return True

    monkeypatch.setattr(cluster, "is_default_home", default)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 1
    assert not (home / "destroy-intent.json").exists()


def test_destroy_refuses_unknown_home(home: Path) -> None:
    other = home.parent / "unclaimed"
    assert lifecycle.cmd_cluster_destroy(path=str(other)) == 1
    assert not other.exists()


def test_down_uses_target_home_environment(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Restore the real down implementation, not the destroy fixture's stop stub.
    import importlib

    importlib.reload(lifecycle)
    seen: list[dict[str, str]] = []

    def run(_argv: object, **kwargs: object):
        seen.append(cast("dict[str, str]", kwargs["env"]))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    # cmd_cluster_down projects the child env straight from the live os.environ
    # (cli/commands/cluster/home.py), not the Settings singleton, so the raw-env
    # seam (not monkeypatch.setenv) is what the code under test actually strips.
    monkeypatch.setitem(os.environ, "AVA_DB_URL", "postgresql://foreign.invalid/foreign")
    assert lifecycle.cmd_cluster_down(path=str(home)) == 0
    assert seen[0]["AVA_HOME"] == str(home)
    assert "AVA_DB_URL" not in seen[0]


@pytest.fixture
def bound_checkout(home: Path) -> Path:
    return home.parent / "checkout"


def _on_detach(monkeypatch: pytest.MonkeyPatch, action: Callable[[], None]) -> None:
    """Run `action` just before destroy writes the detached marker."""
    import base.host.private_storage as storage

    write = storage.write_private_bytes

    def hooked(path: Path, data: bytes, *args: object, **kwargs: object) -> None:
        if b'"detached"' in data:
            action()
        write(path, data, *args, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(storage, "write_private_bytes", hooked)


def test_destroy_retires_exact_checkout_before_detaching(
    home: Path, bound_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pointer = bound_checkout / ".ava_home"

    def retired() -> None:
        assert not pointer.exists()

    _on_detach(monkeypatch, retired)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 0
    assert not pointer.exists()
    assert _detached(home)


@pytest.mark.parametrize("change", ["foreign-home", "symlink", "missing"])
def test_destroy_never_guesses_a_changed_checkout_binding(
    home: Path, bound_checkout: Path, change: str
) -> None:
    pointer = bound_checkout / ".ava_home"
    pointer.unlink()
    foreign = home.parent / "foreign-pointer"
    if change == "foreign-home":
        pointer.write_text(str(home.parent / "neighbor") + "\n")
    elif change == "symlink":
        foreign.write_text(str(home) + "\n")
        pointer.symlink_to(foreign)
    result = lifecycle.cmd_cluster_destroy(path=str(home))
    if change == "missing":
        assert result == 0 and _detached(home)
    else:
        assert result == 1 and not _detached(home)
        assert pointer.exists()
        if change == "symlink":
            assert pointer.is_symlink() and foreign.read_text() == str(home) + "\n"
        else:
            assert pointer.read_text() == str(home.parent / "neighbor") + "\n"


def test_interrupted_pointer_retirement_retries_without_recreating_binding(
    home: Path, bound_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail() -> None:
        raise RuntimeError("interrupted detach")

    with monkeypatch.context() as patched:
        _on_detach(patched, fail)
        with pytest.raises(RuntimeError, match="interrupted detach"):
            lifecycle.cmd_cluster_destroy(path=str(home))
    assert not (bound_checkout / ".ava_home").exists()
    assert not _detached(home)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 0
    assert not (bound_checkout / ".ava_home").exists()
    assert _detached(home)


def test_destroy_preserves_pointer_replaced_during_observation(
    home: Path, bound_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pointer = bound_checkout / ".ava_home"
    read = Path.read_text
    foreign = str(home.parent / "neighbor") + "\n"

    def replace_while_reading(path: Path, **kwargs: object) -> str:
        value = read(path)
        if path == pointer:
            path.unlink()
            path.write_text(foreign)
        return value

    monkeypatch.setattr(Path, "read_text", replace_while_reading)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 1
    assert not _detached(home)
    assert read(pointer) == foreign
