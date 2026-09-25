"""The single start publishes success only after root-owned readiness."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

import cli.commands as cli
from cli.commands import start
from cli.commands._repo import ServiceSpec
from cli.commands._root_driver import LaunchOutcome
from ops.service_spec import _GATEWAY
from shared import start_serving
from shared.exit_codes import SERVICES_NOT_READY_EXIT_CODE

pytestmark = pytest.mark.real_service_readiness_gate


def _ignoring_args[R](action: Callable[[], R]) -> Callable[..., R]:
    def call(*_args: object, **_kwargs: object) -> R:
        return action()

    return call


def _launched(roster: tuple[ServiceSpec, ...], *_args: object, **_kwargs: object) -> LaunchOutcome:
    return LaunchOutcome(roster, ())


def _roster(monkeypatch: pytest.MonkeyPatch, rows: tuple[tuple[str, str | None], ...]) -> None:
    specs = tuple(
        (
            ServiceSpec(
                session=name,
                cmd="unused",
                capabilities=_GATEWAY,
                requires_db=True,
                curl_url="http://127.0.0.1:1/",
            ),
            reason,
        )
        for name, reason in rows
    )
    monkeypatch.setattr(
        "cli.commands._repo._services_for_roles_annotated", _ignoring_args(lambda: specs)
    )

    def selected(_roles: object, skip: set[str]) -> tuple[ServiceSpec, ...]:
        return tuple(spec for spec, reason in specs if reason is None and spec.session not in skip)

    monkeypatch.setattr(cli, "_start_roster", selected)


@pytest.fixture(autouse=True)
def _hermetic_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from cli.commands import (
        _converge_extensions,
        _data_plane,
    )
    from shared import service_selection

    monkeypatch.setattr(service_selection, "selection_path", lambda: tmp_path / "selection.json")
    monkeypatch.setattr(start, "prod_service_checkout_error", _ignoring_args(lambda: None))
    monkeypatch.setattr(start, "_consume_rollout_parent_handoff", lambda: False)
    monkeypatch.setattr(start, "_rollout_child_window", _ignoring_args(lambda: False))
    monkeypatch.setattr(start, "_ensure_gateway_data_plane", lambda: 0)
    monkeypatch.setattr(_data_plane, "prepare_gateway_schema", lambda: None)
    monkeypatch.setattr(_data_plane, "complete_gateway_data_plane", _ignoring_args(lambda: None))
    monkeypatch.setattr(_converge_extensions, "adopt_local_extensions", lambda: None)
    monkeypatch.setattr(_converge_extensions, "materialize_cluster_extensions", lambda: None)
    monkeypatch.setattr(start, "cmd_migrations_apply", _ignoring_args(list[str]))
    monkeypatch.setattr(start, "_refuse_occupied_health_ports", _ignoring_args(lambda: 0))
    monkeypatch.setattr(start, "_record_running_sha", _ignoring_args(lambda: None))
    monkeypatch.setattr(start, "_seed_known_good_if_null", _ignoring_args(lambda: None))
    monkeypatch.setattr(start, "cmd_status", lambda: 0)
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(
        cli,
        "_collect_setup_values",
        _ignoring_args(
            lambda: (
                {
                    "machine_name": "test",
                    "machine_role": "gateway",
                    "gateway_url": "http://127.0.0.1:1",
                },
                list[str](),
            )
        ),
    )
    monkeypatch.setattr(cli, "admit_live_start", _ignoring_args(lambda: False))
    monkeypatch.setattr(cli, "converge_host", _ignoring_args(lambda: None))
    monkeypatch.setattr(cli, "_register_machine_or_die", _ignoring_args(lambda: 0))
    monkeypatch.setattr(cli, "_assert_schema_current_or_die", lambda: 0)
    monkeypatch.setattr(cli, "_probe_gateway_or_die", _ignoring_args(lambda: 0))
    monkeypatch.setattr(cli, "_launch_service_tree", _launched)
    monkeypatch.setattr(
        cli, "_probe_service", _ignoring_args(lambda: cli.ServiceProbe(True, "root", "ready"))
    )

    def wait(roster: tuple[ServiceSpec, ...], **_kwargs: object):
        return cli.ReadinessWait(
            tuple(spec for spec in roster if not cli._probe_service(spec).alive),
            0.0,
            sessions_gone=False,
        )

    monkeypatch.setattr(cli, "_wait_for_service_tree", wait)
    _roster(monkeypatch, (("gateway", None), ("frontend", None)))


def test_unready_frontend_is_failure_and_never_known_good(monkeypatch: pytest.MonkeyPatch) -> None:
    published: list[str] = []

    def probe(spec: ServiceSpec) -> cli.ServiceProbe:
        return cli.ServiceProbe(spec.session != "frontend", "root", "unready")

    monkeypatch.setattr(cli, "_probe_service", probe)
    monkeypatch.setattr(
        start, "_seed_known_good_if_null", _ignoring_args(lambda: published.append("known-good"))
    )
    assert cli.cmd_start() == SERVICES_NOT_READY_EXIT_CODE
    assert not start_serving.is_serving()
    assert published == []


def test_live_repeat_start_never_runs_mutating_preparation(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_prepare(*_args: object, **_kwargs: object) -> None:
        pytest.fail("live repeat start must not converge or migrate")

    monkeypatch.setattr(cli, "admit_live_start", _ignoring_args(lambda: True))
    monkeypatch.setattr(start, "_prepare_cold_start", no_prepare)
    monkeypatch.setattr(
        "shared.cluster.assert_checkpoint_schema_current", _ignoring_args(lambda: None)
    )
    assert cli.cmd_start() == 0


def test_changed_live_generation_refuses_before_selection_or_converge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared import service_selection

    service_selection.resolve_selection({"gateway", "frontend"}, only=("gateway",))
    before = service_selection.selection_path().read_bytes()

    def changed(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("changed source generation")

    monkeypatch.setattr(cli, "admit_live_start", changed)
    monkeypatch.setattr(
        cli, "converge_host", _ignoring_args(lambda: pytest.fail("must refuse before converge"))
    )
    monkeypatch.setattr(
        start, "cmd_migrations_apply", _ignoring_args(lambda: pytest.fail("no live DDL"))
    )
    assert cli.cmd_start(all_services=True) == 1
    assert service_selection.selection_path().read_bytes() == before


def test_live_schema_mismatch_refuses_without_applying_migrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "admit_live_start", _ignoring_args(lambda: True))
    monkeypatch.setattr(cli, "_assert_schema_current_or_die", lambda: 1)
    monkeypatch.setattr(
        start, "cmd_migrations_apply", _ignoring_args(lambda: pytest.fail("no live DDL"))
    )
    monkeypatch.setattr(
        cli, "_launch_service_tree", _ignoring_args(lambda: pytest.fail("no launch"))
    )
    assert cli.cmd_start() == 1


def test_failed_launch_never_becomes_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cli.commands._root_driver.complete_boot_start",
        lambda: pytest.fail("not ready for boot handoff"),
    )

    def failed(roster: tuple[ServiceSpec, ...], *_a: object, **_kw: object) -> LaunchOutcome:
        return LaunchOutcome(roster, ("ava-gateway",))

    monkeypatch.setattr(cli, "_launch_service_tree", failed)
    assert cli.cmd_start() == SERVICES_NOT_READY_EXIT_CODE
    assert not start_serving.is_serving()


def test_cold_preparation_receives_candidate_selection_before_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared import service_selection
    from shared.machine import MachineRoles

    prepared: list[frozenset[str]] = []
    _roster(monkeypatch, (("gateway", None), ("otel-collector", None)))

    def prepare(_repo: Path, _roles: MachineRoles, *, services: frozenset[str]) -> None:
        assert not service_selection.selection_path().exists()
        prepared.append(services)

    monkeypatch.setattr(cli, "converge_host", prepare)
    assert cli.cmd_start(only_services=("gateway",)) == 0
    assert prepared == [frozenset({"gateway"})]
    assert service_selection.read_selection().names == frozenset({"gateway"})


def test_known_good_is_after_serving_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[bool] = []
    monkeypatch.setattr(
        start,
        "_seed_known_good_if_null",
        _ignoring_args(lambda: observed.append(start_serving.is_serving())),
    )
    assert cli.cmd_start() == 0
    assert observed == [True]


def test_lost_generation_never_publishes_known_good(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(start_serving, "mark_serving", _ignoring_args(lambda: False))
    monkeypatch.setattr(
        start,
        "_seed_known_good_if_null",
        _ignoring_args(lambda: pytest.fail("published without custody")),
    )
    assert cli.cmd_start() == 1


def test_only_service_persists_for_repeated_start(monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[tuple[str, ...]] = []

    def launch(roster: tuple[ServiceSpec, ...], *_args: object, **_kwargs: object):
        launched.append(tuple(spec.session for spec in roster))
        return LaunchOutcome(roster, ())

    monkeypatch.setattr(cli, "_launch_service_tree", launch)
    assert cli.cmd_start(only_services=("gateway",)) == 0
    assert cli.cmd_start() == 0
    assert launched == [("gateway",), ("gateway",)]


def test_invalid_selection_never_launches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "_launch_service_tree", _ignoring_args(lambda: pytest.fail("launched invalid roster"))
    )
    with pytest.raises(ValueError, match="unknown"):
        cli.cmd_start(only_services=("typo",))


def test_storage_schema_migration_grants_pooler_precede_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.commands import _data_plane

    steps: list[str] = []
    monkeypatch.setattr(start, "_ensure_gateway_data_plane", lambda: steps.append("storage") or 0)
    monkeypatch.setattr(
        _data_plane, "prepare_gateway_schema", lambda: steps.append("baseline-checkpoints")
    )
    monkeypatch.setattr(
        start, "cmd_migrations_apply", _ignoring_args(lambda: steps.append("migrate") or ["delta"])
    )
    monkeypatch.setattr(
        _data_plane,
        "complete_gateway_data_plane",
        _ignoring_args(lambda: steps.append("grants-pooler-consumer")),
    )

    def launch(roster: tuple[ServiceSpec, ...], *_a: object, **_kw: object) -> LaunchOutcome:
        steps.append("root")
        return LaunchOutcome(roster, ())

    monkeypatch.setattr(cli, "_launch_service_tree", launch)
    assert cli.cmd_start() == 0
    assert steps == ["storage", "baseline-checkpoints", "migrate", "grants-pooler-consumer", "root"]


@pytest.mark.parametrize("ready", [False, True])
def test_boot_manager_handoff_only_after_ready(
    monkeypatch: pytest.MonkeyPatch, ready: bool
) -> None:
    from cli.commands import _root_driver

    calls: list[str] = []
    _roster(monkeypatch, (("frontend", None),))
    monkeypatch.setattr(
        cli, "_probe_service", _ignoring_args(lambda: cli.ServiceProbe(ready, "root", "probe"))
    )
    monkeypatch.setattr(_root_driver, "complete_boot_start", lambda: calls.append("handoff"))
    assert cli.cmd_start() == (0 if ready else SERVICES_NOT_READY_EXIT_CODE)
    assert calls == (["handoff"] if ready else [])


def test_failed_boot_manager_handoff_cannot_report_start_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail():
        raise RuntimeError("manager refused root")

    monkeypatch.setattr("cli.commands._root_driver.complete_boot_start", fail)
    monkeypatch.setattr(
        start,
        "_seed_known_good_if_null",
        _ignoring_args(lambda: pytest.fail("no successful handoff")),
    )
    assert cli.cmd_start() == 1
    assert not start_serving.is_serving()
