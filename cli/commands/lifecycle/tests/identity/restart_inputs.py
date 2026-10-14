"""Explicit invocation inputs and native-boundary fakes for restart command tests."""

from __future__ import annotations

import subprocess as subprocess
from collections.abc import Callable

import pytest

import cli.commands._repo as _repo_commands
import cli.commands._setup as _setup_commands
import cli.commands.lifecycle.root_driver as _root_driver_commands
import cli.commands.lifecycle.stop as _stop_commands
import cli.commands.probe as _probe_commands
from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.deploy.lifecycle.start_serving import RootBirth
from cli import start_runtime
from cli.commands.data_plane import bringup
from cli.commands.lifecycle import converge_host, start
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_fakes import hermetic_gateway_base as hermetic_gateway_base
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

__all__ = ["ignore_retained_children", "restart_command_environment", "root_stop_boundary"]


@pytest.fixture(autouse=True)
def restart_command_environment(
    serving_root: RootBirth, monkeypatch: pytest.MonkeyPatch, *, command_runtime: CommandRuntime
) -> None:
    """cmd_start's multi-machine setup collection + converge_host + register_self
    are all noop in an importing module — here we test session / docker / stop / status call shapes,
    orthogonal to setup. Setup behavior itself is left to base/test_machine.py + the setup-ergonomics tests in `test_commands_start.py`.

    Default role="gateway" (full service set). To test secondary, explicitly override:
        monkeypatch.setattr(_cli, "roles_or_none", lambda *, machine: frozenset({"agent-runner"}))
        monkeypatch.setattr(_cli, "collect_setup_values", lambda: (..., []))"""
    from base.host.runtime_interpreter import LoadedRuntimeIdentity

    def fixture_runtime(_self: object) -> LoadedRuntimeIdentity:
        return serving_root.runtime

    monkeypatch.setattr(start_runtime.StartRuntime, "identity", fixture_runtime)

    def _fake_collect(*, config: ConfigBoot) -> tuple[dict[str, str], list]:
        assert config is command_runtime.config
        return {
            "machine_name": "test-machine",
            "machine_role": "gateway",
            "memory_remote": "git@github.com:test/AvaMemory.git",
            "gateway_url": "http://test-gateway:8000",
        }, []

    monkeypatch.setattr(_setup_commands, "collect_setup_values", _fake_collect)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_root_driver_commands, "admit_live_start", lambda *_a, **_kw: False)  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    monkeypatch.setattr(converge_host, "converge_host", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    # The per-cluster pg/redis bring-up (`ensure_gateway_data_plane`) starts a real
    # native instance under $AVA_HOME. These tests assert session/stop/status call
    # shapes, not infra, so stub it to a noop — keeping them hermetic regardless of
    # the dev host's pg/redis.
    monkeypatch.setattr(start, "cmd_status", ignore_retained_children(lambda: 0))
    from cli.commands.lifecycle import start as _start_mod

    monkeypatch.setattr(
        _start_mod, "ensure_gateway_data_plane", ignore_retained_children(lambda: 0)
    )
    monkeypatch.setattr(bringup, "prepare_gateway_schema", lambda _config: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        bringup,
        "complete_gateway_data_plane",
        lambda _config, **_kw: None,  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )
    from cli.commands.lifecycle.root_driver import LaunchOutcome

    monkeypatch.setattr(
        _root_driver_commands,
        "launch_service_tree",
        lambda roster, *_a, **_kw: LaunchOutcome(roster, ()),  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )
    monkeypatch.setattr(
        _root_driver_commands,
        "wait_for_service_tree",
        lambda *_a, **_kw: _probe_commands.ReadinessWait((), 0.0, sessions_gone=False),  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )

    # roles_or_none (stop/status/converge) + machine_role (cmd_start service
    # resolution) both read the AVA_MACHINE_SERVE_* settings; the test env sets only
    # the agent-runner flag. Pin both to gateway so the default path is the
    # full-service gateway box, deterministic regardless of the dev host's
    # environment. Agent-runner tests override machine_role explicitly.
    def roles(*, machine: MachineIdentity) -> frozenset[str]:
        assert machine is command_runtime.machine
        return frozenset({"gateway"})

    monkeypatch.setattr(_repo_commands, "roles_or_none", roles)
    monkeypatch.setattr(command_runtime.machine, "role", lambda: frozenset({"gateway"}))

    # register_self goes to central DB UPSERT; test does not need real writes. cmd_start goes
    # through register_machine_or_die which internally imports register_self, directly patch the helper to return 0.
    def register(_db: object, _resolved: object, _role: object, *, command: CommandRuntime) -> int:
        del _db, _resolved, _role
        assert command is command_runtime
        return 0

    monkeypatch.setattr(_repo_commands, "register_machine_or_die", register)
    # secondary path will run probe_gateway_or_die; primary does not call it, adding here
    # ensures secondary tests can also reuse the default noop.
    monkeypatch.setattr(_repo_commands, "probe_gateway_or_die", lambda _url: 0)  # pyright: ignore[reportUnknownArgumentType]
    # assert_schema_current_or_die truly calls DB; tests don't need real schema query, directly patch.
    monkeypatch.setattr(_repo_commands, "assert_schema_current_or_die", lambda _db_url: 0)  # pyright: ignore[reportUnknownArgumentType]
    # Root service preparation must not install frontend dependencies in unit tests.
    from cli.commands import _repo
    from cli.commands.lifecycle import root_driver

    monkeypatch.setattr(_repo, "ensure_frontend_deps", lambda _repo: None)  # pyright: ignore[reportUnknownArgumentType]

    # These call-shape tests use the suite's owner DB URL, not an enrolled
    # runner's bootstrap projection. Credential forwarding has its own tests
    # in test_agent_profile_launch_env.py.
    def _fixture_runner_url() -> str:
        return "postgresql://ava_runner:test-runner@127.0.0.1:1/ava_citest"

    monkeypatch.setattr(root_driver, "runner_db_url_projection", _fixture_runner_url)


@pytest.fixture(autouse=True)
def root_stop_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """No command-layer test contacts or signals a native application root."""
    monkeypatch.setattr(_root_driver_commands, "root_tree_plan", lambda _preserve: [])  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    monkeypatch.setattr(_root_driver_commands, "stop_root_service_tree", lambda **_kwargs: 0)  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    monkeypatch.setattr(_stop_commands, "reap_cluster_chrome", lambda: None)
    monkeypatch.setattr(_stop_commands, "force_close_terminals", lambda: None)


def ignore_retained_children[T](callback: Callable[[], T]) -> Callable[..., T]:
    def invoke(*_args: object, **_kwargs: object) -> T:
        return callback()

    return invoke
