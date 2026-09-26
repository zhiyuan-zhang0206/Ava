"""Destroy closes native ownership before freeing a home's port reservation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from cli.commands import cluster_lifecycle as lifecycle
from shared import cluster
from shared.cluster.ports import ClusterPorts
from shared.port_block import PORT_OFFSETS


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    registry = tmp_path / "registry.json"
    monkeypatch.setattr(cluster, "registry_path", lambda: registry)
    rec = cluster.ClusterRecord(
        cast(ClusterPorts, {name: 20000 + offset for name, offset in PORT_OFFSETS.items()}),
        str(home),
        "2026-09-25T00:00:00Z",
    )
    cluster.save_record(rec)

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


def test_destroy_checks_helper_before_freeing_reservation(
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
    assert cluster.get_record(home) is None
    assert '"detached"' in (home / "destroy-intent.json").read_text()


def test_stop_failure_retains_reservation_and_never_unloads_helper(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed_stop(**_kw: object) -> int:
        return 4

    monkeypatch.setattr(lifecycle, "cmd_cluster_down", failed_stop)
    unload = Mock()
    monkeypatch.setattr("services.permissions_helper.launchd_job.unregister_helper", unload)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 4
    assert cluster.get_record(home) is not None
    unload.assert_not_called()


@pytest.mark.parametrize("stage", ["jobs", "helper"])
def test_ambiguous_cleanup_retains_reservation(
    home: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("ownership unknown")

    if stage == "jobs":
        monkeypatch.setattr(lifecycle, "_unregister_scheduled_jobs", fail)
    else:
        monkeypatch.setattr("services.permissions_helper.launchd_job.unregister_helper", fail)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 1
    assert cluster.get_record(home) is not None


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
    assert cluster.get_record(home) is not None


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
    # (cli/commands/cluster_lifecycle.py), not the Settings singleton, so the raw-env
    # seam (not monkeypatch.setenv) is what the code under test actually strips.
    monkeypatch.setitem(os.environ, "AVA_DB_URL", "postgresql://foreign.invalid/foreign")
    assert lifecycle.cmd_cluster_down(path=str(home)) == 0
    assert seen[0]["AVA_HOME"] == str(home)
    assert "AVA_DB_URL" not in seen[0]


@pytest.fixture
def bound_checkout(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from cli.start_identity import IdentityInput, prepare_identity

    checkout = home.parent / "checkout"
    checkout.mkdir()
    cluster.delete_record(home)

    def port_free(_port: int) -> bool:
        return True

    monkeypatch.setattr(cluster, "_port_free", port_free)
    prepare_identity(
        IdentityInput(home, cluster.registry_path(), checkout, True, frozenset({"gateway"}), {})
    )
    return checkout


def test_destroy_retires_exact_checkout_before_freeing_slot(
    home: Path, bound_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pointer = bound_checkout / ".ava_home"
    original = cluster.delete_record_locked

    def delete(target: Path) -> bool:
        assert not pointer.exists()
        return original(target)

    monkeypatch.setattr(cluster, "delete_record_locked", delete)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 0
    assert not pointer.exists()
    assert cluster.get_record(home) is None


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
        assert result == 0 and cluster.get_record(home) is None
    else:
        assert result == 1 and cluster.get_record(home) is not None
        assert pointer.exists()
        if change == "symlink":
            assert pointer.is_symlink() and foreign.read_text() == str(home) + "\n"
        else:
            assert pointer.read_text() == str(home.parent / "neighbor") + "\n"


def test_interrupted_pointer_retirement_retries_without_recreating_binding(
    home: Path, bound_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = cluster.delete_record_locked

    def fail(_home: Path) -> bool:
        raise RuntimeError("interrupted registry release")

    monkeypatch.setattr(cluster, "delete_record_locked", fail)
    with pytest.raises(RuntimeError, match="interrupted registry"):
        lifecycle.cmd_cluster_destroy(path=str(home))
    assert not (bound_checkout / ".ava_home").exists()
    assert cluster.get_record(home) is not None
    monkeypatch.setattr(cluster, "delete_record_locked", original)
    assert lifecycle.cmd_cluster_destroy(path=str(home)) == 0
    assert not (bound_checkout / ".ava_home").exists()


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
    assert cluster.get_record(home) is not None
    assert read(pointer) == foreign
