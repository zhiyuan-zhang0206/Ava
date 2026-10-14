"""The converge firewall step: which binaries it audits, and what it says.

The audit's own decision table is pinned in `base/host/system/tests/test_macos_firewall.py`.
What is asserted here is the step's two jobs on top of it — picking the right set
of serving binaries per capability, and reporting rather than raising.

**Nothing here mutates a real firewall.** The step attempts an unprivileged
mutation first and falls back to `sudo -n`, but every test hands the step a host whose process runner the
mutation goes through (the `Host` the step is handed), so what is asserted is output and
decision-making, never ALF state.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

import cli.commands.converge.firewall as cfw
import cli.commands.converge.firewall_command as firewall_cmd
import cli.commands.lifecycle.converge_host as cv
from base.clock.tests.manual_timers import ManualTimers
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.host.system import macos_firewall as fw
from base.host.system.hostport import Host
from base.host.system.macos_firewall import FirewallAudit, FirewallVerdict
from base.host.system.tests.fake_commands import DARWIN_ARM64, fake_host
from base.paths import otel_collector_binary
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def _ctx(
    home: Path,
    roles: frozenset[str] | None,
    *,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
    host: Host | None = None,
) -> cv.ConvergeCtx:
    machine_config = ConfigBoot()
    return cv.ConvergeCtx(
        repo=Path("/repo"),
        ava_home=home,
        roles=roles,
        config=machine_config,
        database_factory=operator_database,
        producer=producer,
        machine=machine_from_boot(machine_config),
        helper=refuse_helper,
        host=host if host is not None else fake_host(),
        services=frozenset(),
        timers=ManualTimers(),
    )  # type: ignore[arg-type]


def test_step_is_registered_in_converge_for_both_capabilities() -> None:
    """A gateway serves the HTTP port; a runner serves the ops port the gateway dials.

    Both are the same version-stamped interpreter behind the same ALF exposure, so
    role-scoping this to `gateway` would leave the mirror-image outage undiagnosed.
    """
    step = next(s for s in cv.CONVERGE_STEPS if s.apply is cfw.ensure_firewall_allowlist)
    assert step.roles == cv.ALL_ROLES
    # No unit config needed: the audit reads the host, so a fresh install can run it.
    assert step.requires_unit_config is False
    assert step.host_global is False


def test_runner_audits_only_the_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An agent-runner holds no local data plane, so its daemons need no audit."""
    monkeypatch.setattr(
        cfw, "serving_binaries", cfw.serving_binaries
    )  # keep the real implementation
    from base.cluster.dataplane import pg_tools

    monkeypatch.setattr(pg_tools, "pg_tool", lambda name: pytest.fail(f"resolved pg tool {name}"))  # pyright: ignore[reportUnknownArgumentType]
    assert cfw.serving_binaries(frozenset({"agent-runner"}), home=tmp_path) == (
        Path(sys.executable),
    )


def test_gateway_audits_the_data_plane_too(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Postgres and the remote OTLP receiver bind this gateway's off-box address."""
    from base.cluster.dataplane import pg_tools

    pg = tmp_path / "postgres"
    pg.write_text("#!/bin/sh\n")
    otelcol = otel_collector_binary(tmp_path)
    otelcol.parent.mkdir(parents=True)
    otelcol.write_text("#!/bin/sh\n")
    monkeypatch.setattr(pg_tools, "pg_tool", lambda _name: pg)  # pyright: ignore[reportUnknownArgumentType]
    assert cfw.serving_binaries(frozenset({"gateway"}), home=tmp_path) == (
        Path(sys.executable),
        pg,
        otelcol,
    )


def test_nonexistent_resolved_paths_are_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Binary resolvers can fall back to paths that are absent on this host.

    Auditing that phantom path would manufacture a permanent "missing rule" on
    every host without brew, so only paths that exist are audited.
    """
    from base.cluster.dataplane import pg_tools

    monkeypatch.setattr(pg_tools, "pg_tool", lambda name: tmp_path / "nope" / name)  # pyright: ignore[reportUnknownArgumentType]
    # No collector under this home: its binary path resolves to nothing that exists.
    assert cfw.serving_binaries(frozenset({"gateway"}), home=tmp_path) == (Path(sys.executable),)


# --- reporting -------------------------------------------------------------


def _stub_audit(monkeypatch: pytest.MonkeyPatch, audit: FirewallAudit) -> None:
    def fake_audit_this_host(
        _roles: frozenset[str], *, machine: MachineIdentity, host: Host
    ) -> FirewallAudit:
        assert isinstance(machine, MachineIdentity)
        return audit

    monkeypatch.setattr(cfw, "audit_this_host", fake_audit_this_host)  # pyright: ignore[reportUnknownArgumentType]


@pytest.mark.parametrize(
    "verdict",
    [
        FirewallVerdict.NOT_MACOS,
        FirewallVerdict.LOOPBACK_ONLY,
        FirewallVerdict.FIREWALL_OFF,
        FirewallVerdict.ALLOWED,
    ],
)
def test_quiet_on_every_host_that_cannot_have_the_defect(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    verdict: FirewallVerdict,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    """Silence is the contract for a healthy host: converge output an operator
    learns to skim is worthless for finding the one host that is broken."""
    _stub_audit(monkeypatch, FirewallAudit(verdict, "detail"))
    _stub_rules(monkeypatch, {})  # host-independent: CI has no socketfilterfw
    monkeypatch.setattr(fw, "manifest_paths", _empty_manifest)
    _, host = _mutating_result(False)
    cfw.ensure_firewall_allowlist(
        _ctx(
            tmp_path,
            cv.ALL_ROLES,
            operator_database=operator_database,
            producer=operator_pipeline,
            host=host,
        )
    )
    assert capsys.readouterr().err == ""


def _empty_manifest(*, machine_name: str | None, host: Host) -> tuple[Path, ...]:
    assert machine_name is None or isinstance(machine_name, str)
    return ()


def _stub_rules(monkeypatch: pytest.MonkeyPatch, rules: dict[str, bool]) -> None:
    def fake_allowlisted_paths(*, host: Host) -> dict[str, bool]:
        return rules

    monkeypatch.setattr(fw, "allowlisted_paths", fake_allowlisted_paths)


class _Mutations:
    """The runner behind a host: every `socketfilterfw <verb> <path>` (direct or via `sudo -n`)
    goes to `mutate`, which says whether the daemon accepted it."""

    def __init__(self, mutate: Callable[[str, str], bool]) -> None:
        self.mutate = mutate
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        self.calls.append(command)
        accepted = self.mutate(command[-2], command[-1])
        return subprocess.CompletedProcess(command, 0 if accepted else 1, "", "")


def _mutating_host(mutate: Callable[[str, str], bool]) -> tuple[_Mutations, Host]:
    runner = _Mutations(mutate)
    return runner, fake_host(runner, platform=DARWIN_ARM64)


def _mutating_result(ok: bool) -> tuple[_Mutations, Host]:
    return _mutating_host(lambda _verb, _path: ok)


def _skip_verify_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(time, "sleep", no_sleep)


def test_failed_direct_and_sudo_repairs_print_exact_commands_and_do_not_raise(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    """Direct mutation and the old-macOS fallback both fail, so the historical
    manual commands are printed without raising or blocking recovery.
    """
    missing = Path("/uv/cpython-3.12.11/bin/python3.12")
    _stub_audit(
        monkeypatch,
        FirewallAudit(FirewallVerdict.RULES_MISSING, "1 of 1 have no rule", missing=(missing,)),
    )
    _stub_rules(monkeypatch, {})  # nothing allow-listed yet
    monkeypatch.setattr(fw, "manifest_paths", _empty_manifest)  # host-independent
    runner, host = _mutating_result(False)
    cfw.ensure_firewall_allowlist(
        _ctx(
            tmp_path,
            frozenset({"gateway"}),
            operator_database=operator_database,
            producer=operator_pipeline,
            host=host,
        )
    )  # no raise
    err = capsys.readouterr().err
    assert "1 of 1 managed binaries have no ALF allow rule" in err
    assert str(missing) in err
    assert "--add" in err and "--unblockapp" in err
    assert runner.calls == [
        [fw.SOCKETFILTERFW, "--add", str(missing)],
        ["sudo", "-n", fw.SOCKETFILTERFW, "--add", str(missing)],
    ]
    assert "older macOS" in err
    # The rule alone is not enough: an already-bound socket keeps its old policy.
    assert "re-bind" in err
    # The warning states the operator-visible consequence of leaving it unfixed.
    assert "off-box peers cannot reach these services" in err


def test_grant_installed_repairs_silently(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    """With the one-time grant, the missing rule is fixed in place: one line, no
    error block, no commands to paste. The claim is only made after a re-read of
    `--listapps` confirms the rule persisted."""
    missing = Path("/uv/cpython-3.12.11/bin/python3.12")
    _stub_audit(
        monkeypatch,
        FirewallAudit(FirewallVerdict.RULES_MISSING, "1 of 1 have no rule", missing=(missing,)),
    )
    persisted = False

    def rules(*, host: Host) -> dict[str, bool]:
        return {str(missing): True} if persisted else {}

    monkeypatch.setattr(fw, "allowlisted_paths", rules)

    def mutate(verb: str, _path: str) -> bool:
        nonlocal persisted
        persisted = True  # the fake daemon accepts the mutation
        return True

    _, host = _mutating_host(mutate)
    _skip_verify_waits(monkeypatch)
    monkeypatch.setattr(fw, "manifest_paths", _empty_manifest)
    cfw.ensure_firewall_allowlist(
        _ctx(
            tmp_path,
            frozenset({"gateway"}),
            operator_database=operator_database,
            producer=operator_pipeline,
            host=host,
        )
    )
    err = capsys.readouterr().err
    assert "allowed 1 binaries" in err
    assert "--add" not in err  # no manual commands on the repaired path


def test_stale_rules_are_pruned_when_grant_installed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    """A version bump left an orphaned rule; with the grant it is removed."""
    _stub_audit(monkeypatch, FirewallAudit(FirewallVerdict.ALLOWED, "all allow-listed"))
    _stub_rules(monkeypatch, {})
    monkeypatch.setattr(fw, "manifest_paths", _empty_manifest)
    monkeypatch.setattr(
        fw,
        "stale_manifest_rules",
        lambda _rules: (Path("/opt/homebrew/Cellar/node/25.6.1/bin/node"),),  # pyright: ignore[reportUnknownArgumentType]
    )
    _, host = _mutating_result(True)
    cfw.ensure_firewall_allowlist(
        _ctx(
            tmp_path,
            cv.ALL_ROLES,
            operator_database=operator_database,
            producer=operator_pipeline,
            host=host,
        )
    )
    err = capsys.readouterr().err
    assert "removed 1 stale allow rules" in err


def test_prune_runs_before_repair_so_replacement_rules_persist(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    """macOS 15's daemon drops an add whose bundle identifier already has a
    rule, so a stale rule must be pruned before the replacement version is
    added — in the same pass, not on the next converge.

    The fake daemon models the dedup: an `--add` is a silent no-op while the
    stale rule for the same identifier exists, and only persists after that
    rule is removed. The repair succeeding at all therefore proves prune ran
    first; if the step repaired before pruning, the add would be dropped and
    the run would report the failure instead.
    """
    missing = Path("/uv/cpython-3.13.0/bin/python3.13")
    stale = Path("/uv/cpython-3.12.12/bin/python3.12")
    _stub_audit(
        monkeypatch,
        FirewallAudit(FirewallVerdict.RULES_MISSING, "1 of 1 have no rule", missing=(missing,)),
    )
    state = {str(stale): True}

    def fake_allowlisted_paths(*, host: Host) -> dict[str, bool]:
        return dict(state)

    monkeypatch.setattr(fw, "allowlisted_paths", fake_allowlisted_paths)

    def manifest(*, machine_name: str | None, host: Host) -> tuple[Path, ...]:
        assert machine_name is None or isinstance(machine_name, str)
        return (missing,)

    monkeypatch.setattr(fw, "manifest_paths", manifest)
    monkeypatch.setattr(fw, "stale_manifest_rules", lambda _rules: (stale,))  # pyright: ignore[reportUnknownArgumentType]
    _skip_verify_waits(monkeypatch)
    order: list[tuple[str, str]] = []

    def mutate(verb: str, path: str) -> bool:
        order.append((verb, path))
        if verb == "--remove":
            state.pop(path, None)
        elif str(stale) in state:
            pass  # identifier collision: daemon accepts the add, persists nothing
        else:
            state[path] = True
        return True

    _, host = _mutating_host(mutate)
    cfw.ensure_firewall_allowlist(
        _ctx(
            tmp_path,
            cv.ALL_ROLES,
            operator_database=operator_database,
            producer=operator_pipeline,
            host=host,
        )
    )
    assert order[0] == ("--remove", str(stale))  # prune frees the identifier first
    err = capsys.readouterr().err
    assert "allowed 1 binaries" in err  # the add persisted only because prune ran first
    assert "removed 1 stale allow rules" in err


def test_unreadable_says_so_instead_of_claiming_healthy(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    """Silence would assert a clean bill of health the step did not establish."""
    _stub_audit(monkeypatch, FirewallAudit(FirewallVerdict.UNREADABLE, "could not read the state"))
    cfw.ensure_firewall_allowlist(
        _ctx(
            tmp_path, cv.ALL_ROLES, operator_database=operator_database, producer=operator_pipeline
        )
    )
    err = capsys.readouterr().err
    assert "could not read the state" in err
    assert "--add" not in err  # no repair is offered for an unknown state


def test_unconfigured_unit_audits_the_interpreter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    """`roles is None` is a fresh install — no capabilities, but still an interpreter."""
    seen: list[frozenset[str]] = []

    def fake_audit_this_host(
        roles: frozenset[str], *, machine: MachineIdentity, host: Host
    ) -> FirewallAudit:
        assert isinstance(machine, MachineIdentity)
        seen.append(roles)
        return FirewallAudit(FirewallVerdict.ALLOWED, "ok")

    monkeypatch.setattr(
        cfw,
        "audit_this_host",
        fake_audit_this_host,  # pyright: ignore[reportUnknownArgumentType]
    )
    _stub_rules(monkeypatch, {})
    monkeypatch.setattr(fw, "manifest_paths", _empty_manifest)
    _, host = _mutating_result(False)
    cfw.ensure_firewall_allowlist(
        _ctx(
            tmp_path,
            None,
            operator_database=operator_database,
            producer=operator_pipeline,
            host=host,
        )
    )
    assert seen == [frozenset()]


def test_firewall_status_renders_manifest_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    def read_roles(*, machine: MachineIdentity) -> frozenset[str] | None:
        del machine
        return frozenset({"gateway"})

    import cli.commands._repo as _repo_commands

    monkeypatch.setattr(_repo_commands, "roles_or_none", read_roles)

    def fake_audit_this_host(
        _roles: frozenset[str], *, machine: MachineIdentity, host: Host
    ) -> FirewallAudit:
        assert isinstance(machine, MachineIdentity)
        return FirewallAudit(FirewallVerdict.ALLOWED, "all allow-listed")

    monkeypatch.setattr(
        firewall_cmd,
        "audit_this_host",
        fake_audit_this_host,  # pyright: ignore[reportUnknownArgumentType]
    )

    def fake_allowlisted_paths(*, host: Host) -> dict[str, bool]:
        return {"/bin/listener": True}

    monkeypatch.setattr(fw, "allowlisted_paths", fake_allowlisted_paths)

    def render_manifest_status(
        rules: dict[str, bool], *, machine_name: str | None, host: Host
    ) -> str:
        assert machine_name is None or isinstance(machine_name, str)
        return f"  rendered manifest with {len(rules)} rule"

    monkeypatch.setattr(fw, "render_manifest_status", render_manifest_status)
    monkeypatch.setattr(fw, "stale_manifest_rules", lambda _rules: ())  # pyright: ignore[reportUnknownArgumentType]

    def fake_sudo_grant_installed(*, host: Host) -> bool:
        return False

    monkeypatch.setattr(fw, "sudo_grant_installed", fake_sudo_grant_installed)
    assert firewall_cmd.cmd_firewall_status(machine=command_runtime.machine, host=fake_host()) == 0
    output = capsys.readouterr().out
    assert "rendered manifest with 1 rule" in output
    assert "sudo fallback grant" in output
