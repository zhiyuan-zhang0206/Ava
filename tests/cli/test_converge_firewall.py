"""The converge firewall step: which binaries it audits, and what it says.

The audit's own decision table is pinned in `tests/shared/test_macos_firewall.py`.
What is asserted here is the step's two jobs on top of it — picking the right set
of serving binaries per capability, and reporting rather than raising.

**Nothing here mutates a real firewall.** The step attempts an unprivileged
mutation first and falls back to `sudo -n`, but every test stubs the mutation seam
(`shared.macos_firewall._sudo_mutate` / `run_bounded`), so what is asserted is
output and decision-making, never ALF state.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import cli.commands._converge as cv
import cli.commands._converge_firewall as cfw
import cli.commands._firewall as firewall_cmd
from shared import macos_firewall as fw
from shared.macos_firewall import FirewallAudit, FirewallVerdict


def _ctx(home: Path, roles: frozenset[str] | None) -> cv.ConvergeCtx:
    return cv.ConvergeCtx(repo=Path("/repo"), ava_home=home, roles=roles)  # type: ignore[arg-type]


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


def test_runner_audits_only_the_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent-runner holds no local data plane, so its daemons need no audit."""
    monkeypatch.setattr(
        cfw, "serving_binaries", cfw.serving_binaries
    )  # keep the real implementation
    from shared import pg_tools

    monkeypatch.setattr(pg_tools, "pg_tool", lambda name: pytest.fail(f"resolved pg tool {name}"))  # pyright: ignore[reportUnknownArgumentType]
    assert cfw.serving_binaries(frozenset({"agent-runner"})) == (Path(sys.executable),)


def test_gateway_audits_the_data_plane_too(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Postgres and the remote OTLP receiver bind this gateway's off-box address."""
    from shared import paths, pg_tools

    pg = tmp_path / "postgres"
    pg.write_text("#!/bin/sh\n")
    otelcol = tmp_path / "otelcol-contrib"
    otelcol.write_text("#!/bin/sh\n")
    monkeypatch.setattr(pg_tools, "pg_tool", lambda _name: pg)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(paths, "otel_collector_binary", lambda: otelcol)
    assert cfw.serving_binaries(frozenset({"gateway"})) == (
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
    from shared import paths, pg_tools

    monkeypatch.setattr(pg_tools, "pg_tool", lambda name: tmp_path / "nope" / name)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        paths, "otel_collector_binary", lambda: tmp_path / "nope" / "otelcol-contrib"
    )
    assert cfw.serving_binaries(frozenset({"gateway"})) == (Path(sys.executable),)


# --- reporting -------------------------------------------------------------


def _stub_audit(monkeypatch: pytest.MonkeyPatch, audit: FirewallAudit) -> None:
    monkeypatch.setattr(cfw, "audit_this_host", lambda _roles: audit)  # pyright: ignore[reportUnknownArgumentType]


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
) -> None:
    """Silence is the contract for a healthy host: converge output an operator
    learns to skim is worthless for finding the one host that is broken."""
    _stub_audit(monkeypatch, FirewallAudit(verdict, "detail"))
    _stub_rules(monkeypatch, {})  # host-independent: CI has no socketfilterfw
    monkeypatch.setattr(fw, "manifest_paths", lambda: ())
    _stub_sudo_mutate(monkeypatch, False)
    cfw.ensure_firewall_allowlist(_ctx(tmp_path, cv.ALL_ROLES))
    assert capsys.readouterr().err == ""


def _stub_rules(monkeypatch: pytest.MonkeyPatch, rules: dict[str, bool]) -> None:
    monkeypatch.setattr(fw, "allowlisted_paths", lambda: rules)


def _stub_sudo_mutate(monkeypatch: pytest.MonkeyPatch, ok: bool) -> None:
    monkeypatch.setattr(fw, "_sudo_mutate", lambda _verb, _path: ok)  # pyright: ignore[reportUnknownArgumentType]


def test_failed_direct_and_sudo_repairs_print_exact_commands_and_do_not_raise(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
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
    monkeypatch.setattr(fw, "manifest_paths", lambda: ())  # host-independent
    calls: list[list[str]] = []

    def fail(cmd: list[str], **_kw: object):
        calls.append(cmd)
        return type("R", (), {"returncode": 1})()

    monkeypatch.setattr(fw, "run_bounded", fail)
    cfw.ensure_firewall_allowlist(_ctx(tmp_path, frozenset({"gateway"})))  # no raise
    err = capsys.readouterr().err
    assert "1 of 1 managed binaries have no ALF allow rule" in err
    assert str(missing) in err
    assert "--add" in err and "--unblockapp" in err
    assert calls == [
        [fw.SOCKETFILTERFW, "--add", str(missing)],
        ["sudo", "-n", fw.SOCKETFILTERFW, "--add", str(missing)],
    ]
    assert "older macOS" in err
    # The rule alone is not enough: an already-bound socket keeps its old policy.
    assert "re-bind" in err
    # Both halves of the diagnosis name each other. An operator who skims this warning
    # and meets the verdict later needs the two connected; the gate's own detail names
    # `ava converge` in the other direction (test_phase_b_gateway_ready.py).
    assert "OFF_BOX_UNREACHABLE" in err


def test_grant_installed_repairs_silently(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
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

    def rules() -> dict[str, bool]:
        return {str(missing): True} if persisted else {}

    monkeypatch.setattr(fw, "allowlisted_paths", rules)

    def mutate(verb: str, _path: str) -> bool:
        nonlocal persisted
        persisted = True  # the fake daemon accepts the mutation
        return True

    monkeypatch.setattr(fw, "_sudo_mutate", mutate)
    monkeypatch.setattr(fw, "_VERIFY_RETRY_S", 0.0)
    monkeypatch.setattr(fw, "manifest_paths", lambda: ())
    cfw.ensure_firewall_allowlist(_ctx(tmp_path, frozenset({"gateway"})))
    err = capsys.readouterr().err
    assert "allowed 1 binaries" in err
    assert "--add" not in err  # no manual commands on the repaired path


def test_stale_rules_are_pruned_when_grant_installed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A version bump left an orphaned rule; with the grant it is removed."""
    _stub_audit(monkeypatch, FirewallAudit(FirewallVerdict.ALLOWED, "all allow-listed"))
    _stub_rules(monkeypatch, {})
    monkeypatch.setattr(fw, "manifest_paths", lambda: ())
    monkeypatch.setattr(
        fw,
        "stale_manifest_rules",
        lambda _rules: (Path("/opt/homebrew/Cellar/node/25.6.1/bin/node"),),  # pyright: ignore[reportUnknownArgumentType]
    )
    _stub_sudo_mutate(monkeypatch, True)
    cfw.ensure_firewall_allowlist(_ctx(tmp_path, cv.ALL_ROLES))
    err = capsys.readouterr().err
    assert "removed 1 stale allow rules" in err


def test_prune_runs_before_repair_so_replacement_rules_persist(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
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
    monkeypatch.setattr(fw, "allowlisted_paths", lambda: dict(state))
    monkeypatch.setattr(fw, "manifest_paths", lambda: (missing,))
    monkeypatch.setattr(fw, "stale_manifest_rules", lambda _rules: (stale,))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(fw, "_VERIFY_RETRY_S", 0.0)
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

    monkeypatch.setattr(fw, "_sudo_mutate", mutate)
    cfw.ensure_firewall_allowlist(_ctx(tmp_path, cv.ALL_ROLES))
    assert order[0] == ("--remove", str(stale))  # prune frees the identifier first
    err = capsys.readouterr().err
    assert "allowed 1 binaries" in err  # the add persisted only because prune ran first
    assert "removed 1 stale allow rules" in err


def test_unreadable_says_so_instead_of_claiming_healthy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Silence would assert a clean bill of health the step did not establish."""
    _stub_audit(monkeypatch, FirewallAudit(FirewallVerdict.UNREADABLE, "could not read the state"))
    cfw.ensure_firewall_allowlist(_ctx(tmp_path, cv.ALL_ROLES))
    err = capsys.readouterr().err
    assert "could not read the state" in err
    assert "--add" not in err  # no repair is offered for an unknown state


def test_unconfigured_unit_audits_the_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`roles is None` is a fresh install — no capabilities, but still an interpreter."""
    seen: list[frozenset[str]] = []
    monkeypatch.setattr(
        cfw,
        "audit_this_host",
        lambda roles: seen.append(roles) or FirewallAudit(FirewallVerdict.ALLOWED, "ok"),  # pyright: ignore[reportUnknownArgumentType]
    )
    _stub_rules(monkeypatch, {})
    monkeypatch.setattr(fw, "manifest_paths", lambda: ())
    _stub_sudo_mutate(monkeypatch, False)
    cfw.ensure_firewall_allowlist(_ctx(tmp_path, None))
    assert seen == [frozenset()]


def test_firewall_status_renders_manifest_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import cli.commands as command_namespace

    monkeypatch.setattr(command_namespace, "_roles_or_none", lambda: {"gateway"})
    monkeypatch.setattr(
        firewall_cmd,
        "audit_this_host",
        lambda _roles: FirewallAudit(FirewallVerdict.ALLOWED, "all allow-listed"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(fw, "allowlisted_paths", lambda: {"/bin/listener": True})
    monkeypatch.setattr(
        fw,
        "render_manifest_status",
        lambda rules: f"  rendered manifest with {len(rules)} rule",  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(fw, "stale_manifest_rules", lambda _rules: ())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(fw, "sudo_grant_installed", lambda: False)
    assert firewall_cmd.cmd_firewall_status() == 0
    output = capsys.readouterr().out
    assert "rendered manifest with 1 rule" in output
    assert "sudo fallback grant" in output
