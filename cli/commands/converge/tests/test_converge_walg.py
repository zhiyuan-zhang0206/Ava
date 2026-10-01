"""The WAL-G converge step: inert while off, strict before Postgres starts while on."""

from __future__ import annotations

from pathlib import Path

import pytest

from base.config import settings
from cli.commands.converge import host as converge_host
from cli.commands.converge import walg as converge_walg_module
from cli.commands.converge.spec import ConvergeCtx
from services.gateway_side.walg import config as walg_config
from services.gateway_side.walg.tests.support import Sandbox, make_sandbox, valid_config


@pytest.fixture
def installs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record binary installs instead of downloading anything."""
    installed: list[str] = []
    monkeypatch.setattr(
        converge_walg_module, "ensure_walg_binary", lambda: installed.append("wal-g") or Path("x")
    )
    return installed


def _remote(monkeypatch: pytest.MonkeyPatch, *, remote: bool) -> None:
    """`is_remote` is derived from the URLs; the converge step only reads the verdict."""
    monkeypatch.setattr(type(settings.data_plane), "is_remote", property(lambda _self: remote))


def _ctx(sandbox: Sandbox) -> ConvergeCtx:
    return ConvergeCtx(repo=sandbox.home, ava_home=sandbox.home, roles=frozenset({"gateway"}))


def test_the_step_sits_between_the_postgres_runtime_and_the_pooler_on_gateways() -> None:
    names = [step.name for step in converge_host.CONVERGE_STEPS]
    step = converge_host.CONVERGE_STEPS[names.index("WAL-G archiving")]

    assert step.apply is converge_walg_module.converge_walg
    assert step.roles == frozenset({"gateway"})
    assert step.host_global is False
    assert names.index("PostgreSQL 17 + pgvector runtime") < names.index("WAL-G archiving")
    assert names.index("WAL-G archiving") < names.index(
        "one DB URL + pgbouncer binary (when enabled)"
    )


def test_off_installs_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installs: list[str]
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch, enabled=False)
    _remote(monkeypatch, remote=False)

    converge_walg_module.converge_walg(_ctx(sandbox))

    assert installs == []
    assert not (sandbox.home / "backups").exists()


def test_on_installs_validates_pins_the_key_and_makes_the_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installs: list[str]
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    _remote(monkeypatch, remote=False)

    converge_walg_module.converge_walg(_ctx(sandbox))

    assert installs == ["wal-g"]
    assert (
        walg_config.pinned_key_id() == walg_config.read_config(sandbox.config_file).key_fingerprint
    )
    state = sandbox.home / "backups" / "walg"
    assert state.is_dir() and state.stat().st_mode & 0o777 == 0o700


def test_a_bad_configuration_fails_converge_not_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installs: list[str]
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    _remote(monkeypatch, remote=False)
    sandbox.write_config(valid_config(sandbox.key_file, WALG_PREVENT_WAL_OVERWRITE="false"))

    with pytest.raises(walg_config.WalgConfigError, match="WALG_PREVENT_WAL_OVERWRITE"):
        converge_walg_module.converge_walg(_ctx(sandbox))

    assert walg_config.pinned_key_id() is None, "an unusable configuration pins nothing"


def test_a_different_key_than_the_pinned_one_fails_converge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installs: list[str]
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    _remote(monkeypatch, remote=False)
    converge_walg_module.converge_walg(_ctx(sandbox))
    sandbox.key_file.write_text("cd" * 32 + "\n")

    with pytest.raises(walg_config.WalgConfigError, match="is not the key this home pinned"):
        converge_walg_module.converge_walg(_ctx(sandbox))


def test_a_remote_managed_data_plane_cannot_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installs: list[str]
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    _remote(monkeypatch, remote=True)

    with pytest.raises(RuntimeError, match="remote-managed"):
        converge_walg_module.converge_walg(_ctx(sandbox))

    assert installs == []


# ── the daily tick job follows the key ───────────────────────────────────────


@pytest.fixture
def job_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the registrar calls: nothing here reaches a scheduler."""
    from base.host.system import walg_job

    calls: list[str] = []
    monkeypatch.setattr(walg_job, "register_walg_job", lambda: calls.append("register"))
    monkeypatch.setattr(walg_job, "unregister_walg_job", lambda: calls.append("unregister"))
    return calls


def test_the_job_step_runs_on_gateways_after_the_other_scheduled_jobs() -> None:
    from cli.commands.converge._os_jobs import ensure_walg_job

    names = [step.name for step in converge_host.CONVERGE_STEPS]
    step = converge_host.CONVERGE_STEPS[names.index("WAL-G backup job")]

    assert step.apply is ensure_walg_job
    assert step.roles == frozenset({"gateway"})
    assert names.index("PR flow sampler job") < names.index("WAL-G backup job")


def test_the_key_being_set_registers_the_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, job_calls: list[str]
) -> None:
    from cli.commands.converge._os_jobs import ensure_walg_job

    sandbox = make_sandbox(tmp_path, monkeypatch)

    ensure_walg_job(_ctx(sandbox))

    assert job_calls == ["register"]


def test_the_key_being_unset_removes_the_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, job_calls: list[str]
) -> None:
    from cli.commands.converge._os_jobs import ensure_walg_job

    sandbox = make_sandbox(tmp_path, monkeypatch, enabled=False)

    ensure_walg_job(_ctx(sandbox))

    assert job_calls == ["unregister"]
