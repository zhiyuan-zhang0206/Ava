"""shared.os_schtasks — the Windows scheduler primitives.

Pins the decisions that would silently degrade the Windows experience if a later
edit undid them (a console flash every 60s; a job running the wrong interpreter;
an OS-level supervisor that stops the moment a laptop is unplugged), plus the
per-cluster scoping that keeps two clusters' jobs apart.

**These are configuration assertions, not behavioural ones.** CI has no Windows
Task Scheduler, so nothing here proves a registered task *runs* the way its
definition says — only that the definition this repo hands to
`schtasks /Create /XML` asks for what it means to ask for. The runtime side is
verified on the reference box.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

import pytest

from shared import os_schtasks as st

# A path with a space in it, because a Windows user profile may contain one
# (`C:\Users\First Last\...`) and `<Command>` must carry it unquoted.
_PYTHONW = Path(r"C:\Users\First Last\.ava\source\.venv\Scripts\pythonw.exe")

_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


@pytest.fixture(autouse=True)
def _stable_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(st, "_home_slug", lambda: "ava-deadbeef")
    monkeypatch.setattr(st, "_task_user", lambda: r"WIN-BOX\ava")


class _ScriptsBackend:
    """Minimal stand-in: the only backend method the interpreter probe consults."""

    def venv_bin_dir_name(self) -> str:
        return "Scripts"


def _scripts_backend() -> _ScriptsBackend:
    return _ScriptsBackend()


def _ok(**kw: object):  # type: ignore[no-untyped-def]
    return type("R", (), {"returncode": 0, "stdout": "", "stderr": "", **kw})()


@pytest.fixture
def registered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[list[str], str]]:
    """Captures every `(argv, definition)` a registration would hand to schtasks.

    The definition is read back off disk through `encoding="utf-16"`, so a test
    that reads it at all has already proved the file was written in the encoding
    Task Scheduler's own exports use.
    """
    calls: list[tuple[list[str], str]] = []
    xml_dir = tmp_path / "schtasks"
    xml_dir.mkdir()
    monkeypatch.setattr(st, "_xml_path", lambda kind: xml_dir / f"{kind}.xml")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(st, "_pythonw", lambda: _PYTHONW)

    def _fake_run(argv: list[str]):  # type: ignore[no-untyped-def]
        path = Path(argv[argv.index("/XML") + 1])
        calls.append((argv, path.read_text(encoding="utf-16")))
        return _ok()

    monkeypatch.setattr(st, "_run", _fake_run)
    return calls


def _parse(definition: str) -> ET.Element:
    return ET.fromstring(definition)  # noqa: S314 — self-generated, no external input


def _settings(definition: str) -> dict[str, str]:
    """The `<Settings>` block as a flat name -> text mapping."""
    block = _parse(definition).find("t:Settings", _NS)
    assert block is not None, definition
    return {el.tag.split("}")[1]: (el.text or "") for el in block}


# --- naming ---------------------------------------------------------------


def test_task_name_is_folder_scoped_per_cluster() -> None:
    """`\\Ava\\<slug>\\<kind>` mirrors the launchd label — two clusters sharing a
    home basename must not collide on one task."""
    assert st.task_name("watchdog-probe-gateway") == r"\Ava\ava-deadbeef\watchdog-probe-gateway"


def test_task_names_differ_per_kind() -> None:
    assert st.task_name("health-probe") != st.task_name("autostart")


# --- the action -----------------------------------------------------------


def test_action_uses_pythonw_not_ava_exe(registered: list[tuple[list[str], str]]) -> None:
    """`ava.exe` is a console entry point: a once-a-minute job would flash a
    window forever. pythonw.exe is the windowless launcher."""
    st.create_minute_task(
        "watchdog-probe-gateway", ("cluster", "watchdog-probe"), 1, time_limit_s=300
    )
    definition = registered[0][1]
    exec_el = _parse(definition).find("t:Actions/t:Exec", _NS)
    assert exec_el is not None
    assert exec_el.findtext("t:Command", namespaces=_NS) == str(_PYTHONW)
    assert "ava.exe" not in definition
    assert exec_el.findtext("t:Arguments", namespaces=_NS) == "-m cli.main cluster watchdog-probe"


def test_command_is_not_hand_quoted(registered: list[tuple[list[str], str]]) -> None:
    """`<Command>` is its own element, so Task Scheduler takes the whole text as
    one path however many spaces it holds — added quotes would become part of it.
    (The single-string `/TR` form this replaced did need them.)"""
    st.create_logon_task("autostart", ("boot",), time_limit_s=st.NO_TIME_LIMIT_S)
    assert f"<Command>{_PYTHONW}</Command>" in registered[0][1]


def test_interpreter_falls_back_to_python_without_pythonw(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stripped env without pythonw.exe should still run — a console flash
    beats not running at all."""
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    monkeypatch.setattr("shared.paths.repo_root", lambda: tmp_path)
    monkeypatch.setattr("shared.platform_backend.get_backend", _scripts_backend)
    assert st._pythonw().name == "python.exe"


def test_interpreter_prefers_pythonw_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "pythonw.exe").write_text("")
    monkeypatch.setattr("shared.paths.repo_root", lambda: tmp_path)
    monkeypatch.setattr("shared.platform_backend.get_backend", _scripts_backend)
    assert st._pythonw().name == "pythonw.exe"


# --- the hardened settings ------------------------------------------------


def test_the_definition_is_well_formed_xml(registered: list[tuple[list[str], str]]) -> None:
    """A malformed definition is rejected by schtasks at registration, which fails
    converge — so the shape is worth pinning here rather than on the box."""
    st.create_minute_task("health-probe", ("cluster", "health-probe"), 5, time_limit_s=1800)
    root = _parse(registered[0][1])
    assert root.tag.endswith("}Task")
    assert root.attrib["version"] == "1.2"


def test_power_settings_are_explicit_not_inherited(
    registered: list[tuple[list[str], str]],
) -> None:
    """The defect this module was hardened for: Task Scheduler's defaults stop a
    minute-cadence job when a laptop goes on battery, refuse to start it while on
    battery, and never catch up a missed tick — which made the whole OS-level
    self-healing chain conditional on the power cable."""
    st.create_minute_task(
        "watchdog-probe-agent-runner", ("cluster", "watchdog-probe"), 1, time_limit_s=300
    )
    settings = _settings(registered[0][1])
    assert settings["DisallowStartIfOnBatteries"] == "false"
    assert settings["StopIfGoingOnBatteries"] == "false"
    assert settings["StartWhenAvailable"] == "true"
    # Deliberately NOT true: waking a sleeping laptop every 60s would be hostile;
    # the resume case is covered by StartWhenAvailable instead.
    assert settings["WakeToRun"] == "false"


def test_ignore_new_is_paired_with_a_hard_terminate(
    registered: list[tuple[list[str], str]],
) -> None:
    """`IgnoreNew` keeps cadence invocations from piling up, which means the
    execution time limit is the ONLY thing that stops one wedged invocation from
    blocking every later one — and the limit can only end it if the scheduler is
    allowed to terminate hard."""
    st.create_minute_task(
        "watchdog-probe-agent-runner", ("cluster", "watchdog-probe"), 1, time_limit_s=300
    )
    settings = _settings(registered[0][1])
    assert settings["MultipleInstancesPolicy"] == "IgnoreNew"
    assert settings["AllowHardTerminate"] == "true"


def test_the_principal_stays_the_interactive_user(
    registered: list[tuple[list[str], str]],
) -> None:
    """InteractiveToken is what keeps registration password-free and runs the job
    as the user that owns $AVA_HOME; LeastPrivilege restates what a bare
    `/Create` produced, so this is not a silent elevation."""
    st.create_logon_task("autostart", ("boot",), time_limit_s=st.NO_TIME_LIMIT_S)
    principal = _parse(registered[0][1]).find("t:Principals/t:Principal", _NS)
    assert principal is not None
    assert principal.findtext("t:LogonType", namespaces=_NS) == "InteractiveToken"
    assert principal.findtext("t:RunLevel", namespaces=_NS) == "LeastPrivilege"
    assert principal.findtext("t:UserId", namespaces=_NS) == r"WIN-BOX\ava"


@pytest.mark.parametrize(
    ("seconds", "want"),
    [(0, "PT0S"), (300, "PT5M"), (1800, "PT30M"), (3600, "PT1H"), (90, "PT90S")],
)
def test_iso_duration_reads_like_an_exported_task(seconds: int, want: str) -> None:
    """Whole minutes/hours are emitted as such so the artifact can be diffed
    against `schtasks /Query /XML`. Zero is the schema's "no limit"."""
    assert st.iso_duration(seconds) == want


def test_a_negative_time_limit_explodes() -> None:
    """A nonsense bound is a caller bug; normalising it would hide which job got
    the wrong one."""
    with pytest.raises(ValueError, match="must not be negative"):
        st.iso_duration(-1)


def test_the_time_limit_reaches_the_definition(registered: list[tuple[list[str], str]]) -> None:
    st.create_minute_task("health-probe", ("cluster", "health-probe"), 5, time_limit_s=1800)
    assert _settings(registered[0][1])["ExecutionTimeLimit"] == "PT30M"


def test_no_time_limit_is_expressible(registered: list[tuple[list[str], str]]) -> None:
    """The boot job must NOT be bounded — `cli/boot_retry.py` retries with no
    attempt cap by design, and a finite limit would reimpose one on Windows
    alone."""
    st.create_logon_task("autostart", ("boot",), time_limit_s=st.NO_TIME_LIMIT_S)
    assert _settings(registered[0][1])["ExecutionTimeLimit"] == "PT0S"


# --- triggers -------------------------------------------------------------


def test_minute_task_repeats_forever_at_the_asked_interval(
    registered: list[tuple[list[str], str]],
) -> None:
    """An omitted `<Duration>` is the schema's "repeat indefinitely"; a fixed
    StartBoundary keeps the occurrence grid stable across re-registrations."""
    st.create_minute_task("health-probe", ("cluster", "health-probe"), 5, time_limit_s=1800)
    trigger = _parse(registered[0][1]).find("t:Triggers/t:TimeTrigger", _NS)
    assert trigger is not None
    assert trigger.findtext("t:Repetition/t:Interval", namespaces=_NS) == "PT5M"
    assert trigger.find("t:Repetition/t:Duration", _NS) is None
    assert trigger.findtext("t:StartBoundary", namespaces=_NS) == st._START_BOUNDARY


@pytest.mark.parametrize(
    ("asked", "want"), [(0, "PT1M"), (-5, "PT1M"), (1, "PT1M"), (99999, "PT999M")]
)
def test_minute_task_clamps_to_the_schedulers_valid_range(
    registered: list[tuple[list[str], str]], asked: int, want: str
) -> None:
    """Task Scheduler accepts 1-999 minutes; anything outside is rejected
    outright, so clamp rather than emit an interval that registers nothing."""
    st.create_minute_task("k", ("start",), asked, time_limit_s=300)
    found = _parse(registered[0][1]).findtext(
        "t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", namespaces=_NS
    )
    assert found == want


def test_logon_task_triggers_on_logon_not_on_a_clock(
    registered: list[tuple[list[str], str]],
) -> None:
    """A logon trigger (user session), not ONSTART — ONSTART runs as SYSTEM, which
    owns neither $AVA_HOME nor the supervised sessions. The cost is that a reboot
    with no interactive logon does not bring the cluster back."""
    st.create_logon_task("autostart", ("boot",), time_limit_s=st.NO_TIME_LIMIT_S)
    root = _parse(registered[0][1])
    assert root.find("t:Triggers/t:LogonTrigger", _NS) is not None
    assert root.find("t:Triggers/t:TimeTrigger", _NS) is None
    assert root.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=_NS) == r"WIN-BOX\ava"


def test_daily_task_uses_the_requested_local_schedule(
    registered: list[tuple[list[str], str]],
) -> None:
    st.create_daily_task(
        "logs-rotate",
        ("logs", "rotate"),
        hour=4,
        minute=40,
        time_limit_s=1800,
    )

    trigger = _parse(registered[0][1]).find("t:Triggers/t:CalendarTrigger", _NS)
    assert trigger is not None
    assert trigger.findtext("t:StartBoundary", namespaces=_NS) == "2000-01-01T04:40:00"
    assert trigger.findtext("t:ScheduleByDay/t:DaysInterval", namespaces=_NS) == "1"
    assert trigger.find("t:Repetition", _NS) is None


# --- schtasks invocation --------------------------------------------------


def test_registration_replaces_the_existing_task(
    registered: list[tuple[list[str], str]],
) -> None:
    """`/F` is what makes registration idempotent — without it a re-run fails on an
    existing task and an interval or settings change never takes effect."""
    st.create_minute_task("health-probe", ("cluster", "health-probe"), 5, time_limit_s=1800)
    argv = registered[0][0]
    assert argv[:2] == ["schtasks", "/Create"]
    assert argv[argv.index("/TN") + 1] == r"\Ava\ava-deadbeef\health-probe"
    assert "/XML" in argv
    assert "/F" in argv


def test_re_registration_is_byte_identical(registered: list[tuple[list[str], str]]) -> None:
    """Converge re-registers on every `ava start`, so the definition must not
    drift between runs — a wall-clock StartBoundary would re-phase the job each
    time and make a real change indistinguishable from noise."""
    for _ in range(2):
        st.create_minute_task(
            "watchdog-probe-agent-runner", ("cluster", "watchdog-probe"), 1, time_limit_s=300
        )
    assert registered[0][1] == registered[1][1]
    assert registered[0][0] == registered[1][0]


def test_the_definition_is_kept_under_the_cluster_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """It IS the registered configuration, so it lives beside the cluster that
    owns it (inspectable after the fact, and destroyed with the home)."""
    monkeypatch.setattr("shared.paths.run_dir", lambda: tmp_path)
    assert st._xml_path("autostart") == tmp_path / "schtasks" / "autostart.xml"


def test_create_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch, registered: list[tuple[list[str], str]]
) -> None:
    """A rejected definition must fail loudly: converge propagates it, so a
    cluster never comes up believing it is supervised when it is not. The one
    retry happens first (and the delay is skipped in tests)."""
    monkeypatch.setattr(st, "_REGISTER_RETRY_DELAY_S", 0)
    calls: list[list[str]] = []

    def _failing(argv: list[str]):  # type: ignore[no-untyped-def]
        calls.append(argv)
        return _ok(returncode=1, stderr="ERROR: invalid XML")

    monkeypatch.setattr(st, "_run", _failing)
    # The reason travels back, not just a failure flag: it is what the caller puts
    # on the stderr warning an operator actually reads.
    assert st.create_minute_task("k", ("start",), 1, time_limit_s=300) == "ERROR: invalid XML"
    assert (
        st.create_logon_task("k", ("boot",), time_limit_s=st.NO_TIME_LIMIT_S)
        == "ERROR: invalid XML"
    )
    # Both creates retried once: two /Create invocations each.
    assert len(calls) == 4
    assert all(argv[:2] == ["schtasks", "/Create"] for argv in calls)


def test_create_retries_a_transient_failure(
    monkeypatch: pytest.MonkeyPatch, registered: list[tuple[list[str], str]]
) -> None:
    """The win 2026-08-11 failure class is a transient race (concurrent XML
    write, ghost-task instance) — the one retry clears it and the registration
    succeeds, which is what kept that host offline for ~45 minutes."""
    monkeypatch.setattr(st, "_REGISTER_RETRY_DELAY_S", 0)
    attempts = {"n": 0}

    def _flaky(_argv: list[str]):  # type: ignore[no-untyped-def]
        attempts["n"] += 1
        if attempts["n"] == 1:
            return _ok(returncode=87, stderr="ERROR: The parameter is incorrect.")
        return _ok()

    monkeypatch.setattr(st, "_run", _flaky)
    assert st.create_minute_task("k", ("start",), 1, time_limit_s=300) is None
    assert attempts["n"] == 2


def test_delete_is_success_even_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirrors the launchd/crontab unregister paths: nothing registered is a
    no-op success, so a teardown never fails on a job that was never created."""
    monkeypatch.setattr(
        st,
        "_run",
        lambda _argv: _ok(returncode=1, stderr="ERROR: task does not exist"),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert st.delete_task("health-probe", "ava-target") == 0


def test_delete_targets_the_given_slug_not_this_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """The task deleted is the caller's, never one resolved from this process.

    `ava cluster destroy` drives this against another home; a slug resolved here
    would name — and delete — the running cluster's own task instead.
    """
    seen: list[list[str]] = []
    monkeypatch.setattr(st, "_run", lambda argv: seen.append(argv) or _ok())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(st, "_home_slug", lambda: "ava-this-process")

    st.delete_task("health-probe", "ava-target")

    name = seen[0][seen[0].index("/TN") + 1]
    assert name == r"\Ava\ava-target\health-probe"
    assert "ava-this-process" not in name


# --- every registration call site ----------------------------------------
#
# The three `_register_windows` functions are the only callers, and the settings
# above are worthless if one of them bypasses them. Driving each end-to-end (with
# only `schtasks` itself stubbed) is what keeps that honest.


def _register_watchdog_probe() -> str | None:
    from shared import os_watchdog_probe

    return os_watchdog_probe._register_windows("agent-runner", 60)


def _register_health_probe() -> str | None:
    from shared import os_cron

    return os_cron._register_windows(300, 3)


def _register_autostart() -> str | None:
    from shared import os_autostart

    return os_autostart._register_windows()


@pytest.mark.parametrize(
    ("register", "kind", "time_limit"),
    [
        (_register_watchdog_probe, "watchdog-probe-agent-runner", "PT5M"),
        (_register_health_probe, "health-probe", "PT30M"),
        # The boot job is deliberately unbounded — see os_autostart._register_windows.
        (_register_autostart, "autostart", "PT0S"),
    ],
)
def test_every_call_site_gets_the_hardened_settings(
    registered: list[tuple[list[str], str]],
    register: Callable[[], int],
    kind: str,
    time_limit: str,
) -> None:
    assert register() is None
    argv, definition = registered[0]
    assert argv[argv.index("/TN") + 1] == rf"\Ava\ava-deadbeef\{kind}"
    settings = _settings(definition)
    assert settings["DisallowStartIfOnBatteries"] == "false"
    assert settings["StopIfGoingOnBatteries"] == "false"
    assert settings["StartWhenAvailable"] == "true"
    assert settings["AllowHardTerminate"] == "true"
    assert settings["MultipleInstancesPolicy"] == "IgnoreNew"
    assert settings["ExecutionTimeLimit"] == time_limit


@pytest.mark.parametrize(
    ("register", "arguments"),
    [
        (_register_watchdog_probe, "-m cli.main cluster watchdog-probe --role agent-runner"),
        (_register_health_probe, "-m cli.main cluster health-probe --auto-rollback --threshold 3"),
        (_register_autostart, "-m cli.main boot"),
    ],
)
def test_every_call_site_keeps_its_payload(
    registered: list[tuple[list[str], str]],
    register: Callable[[], int],
    arguments: str,
) -> None:
    """The hardening changed how a task is registered, not what it runs."""
    assert register() is None
    found = _parse(registered[0][1]).findtext("t:Actions/t:Exec/t:Arguments", namespaces=_NS)
    assert found == arguments


# --- stale-slug reap --------------------------------------------------------
#
# When a home's slug changes, its old tasks keep firing under the old folder and
# race the current slug's /Create on every converge (win 2026-08-11, task #1196).
# The reap reclaims exactly what no live cluster can own: this home's own legacy
# slug folder and any non-current-shape folder — never another current-shape
# slug folder, which is a co-located cluster's live namespace.

_QUERY_OK = _ok(
    stdout=(
        '"\\Ava\\ava\\watchdog-probe-agent-runner","8/12/2026 10:00:00 AM","Ready"\n'
        '"\\Ava\\ava-b61dd50b\\watchdog-probe-agent-runner","8/12/2026 10:00:00 AM","Ready"\n'
        '"\\Ava\\ava-other-deadbeef\\health-probe","N/A","Ready"\n'
        '"\\Ava\\ava-other\\autostart","N/A","Ready"\n'
        '"\\Ava\\health-probe","N/A","Ready"\n'
    )
)


def _fake_schtasks(monkeypatch: pytest.MonkeyPatch, deletes: list[str]) -> None:
    """Fake `_run` that answers the reap's /Query and records /Delete targets."""

    def _fake(argv: list[str]):  # type: ignore[no-untyped-def]
        if argv[1] == "/Query":
            return _QUERY_OK
        assert argv[1] == "/Delete"
        deletes.append(argv[argv.index("/TN") + 1])
        return _ok()

    monkeypatch.setattr(st, "_run", _fake)


def test_reap_removes_legacy_and_foreign_slug_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ghost class (a bare-basename folder next to the current hash slug) is
    removed, as are non-current-shape folders and root-level tasks; the current
    slug and other current-shape (co-located) slug folders are untouched."""
    monkeypatch.setattr(st, "_home_slug", lambda: "ava-b61dd50b")
    deletes: list[str] = []
    _fake_schtasks(monkeypatch, deletes)

    assert st.reap_stale_tasks() == 3

    assert deletes == [
        r"\Ava\ava\watchdog-probe-agent-runner",  # this home's legacy slug
        r"\Ava\ava-other\autostart",  # not current-shape — no live cluster
        r"\Ava\health-probe",  # root of \Ava\ — no slug folder at all
    ]


def test_reap_keeps_other_current_shape_slug_folders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A co-located cluster registers under its own `<basename>-<8-hex>` folder;
    deleting it would deregister a live cluster's jobs. Only the current slug
    folder and legacy/non-shape folders are ever touched."""
    monkeypatch.setattr(st, "_home_slug", lambda: "ava-b61dd50b")
    deletes: list[str] = []
    _fake_schtasks(monkeypatch, deletes)

    st.reap_stale_tasks()

    assert r"\Ava\ava-other-deadbeef\health-probe" not in deletes
    assert r"\Ava\ava-b61dd50b\watchdog-probe-agent-runner" not in deletes


def test_reap_legacy_slug_wins_over_shape_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A home whose basename itself ends in 8 hex chars has a legacy slug that
    LOOKS current-shape (`~/.ava-1234abcd` -> legacy `ava-1234abcd`); the legacy
    check must run before the shape check so its own old tasks are still reaped."""
    monkeypatch.setattr(st, "_home_slug", lambda: "ava-1234abcd-5678abcd")
    query = _ok(
        stdout=(
            '"\\Ava\\ava-1234abcd\\watchdog-probe-agent-runner","N/A","Ready"\n'
            '"\\Ava\\ava-1234abcd-5678abcd\\health-probe","N/A","Ready"\n'
            '"\\Ava\\ava-99999999\\autostart","N/A","Ready"\n'
        )
    )
    deletes: list[str] = []

    def _fake(argv: list[str]):  # type: ignore[no-untyped-def]
        if argv[1] == "/Query":
            return query
        deletes.append(argv[argv.index("/TN") + 1])
        return _ok()

    monkeypatch.setattr(st, "_run", _fake)

    st.reap_stale_tasks()

    # `ava-1234abcd` IS this home's legacy slug — deleted despite matching the
    # current-slug shape; the other hash-slug folder and the current one stay.
    assert deletes == [r"\Ava\ava-1234abcd\watchdog-probe-agent-runner"]


def test_reap_query_failure_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host whose Task Scheduler will not answer must not fail the converge
    that is about to re-register the current tasks."""
    calls: list[list[str]] = []

    def _failing(argv: list[str]):  # type: ignore[no-untyped-def]
        calls.append(argv)
        return _ok(returncode=1, stderr="ERROR: The system cannot find the file specified.")

    monkeypatch.setattr(st, "_run", _failing)
    assert st.reap_stale_tasks() == 0
    assert len(calls) == 1  # no /Delete attempts after a failed /Query


def test_reap_delete_failure_does_not_abort_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One task that will not delete must not stop the rest of the sweep — the
    reap is best-effort and the next converge tries again."""
    monkeypatch.setattr(st, "_home_slug", lambda: "ava-b61dd50b")
    deletes: list[str] = []

    def _flaky(argv: list[str]):  # type: ignore[no-untyped-def]
        if argv[1] == "/Query":
            return _QUERY_OK
        deletes.append(argv[argv.index("/TN") + 1])
        if deletes[-1] == r"\Ava\ava\watchdog-probe-agent-runner":
            return _ok(returncode=1, stderr="ERROR: Access is denied.")
        return _ok()

    monkeypatch.setattr(st, "_run", _flaky)
    assert st.reap_stale_tasks() == 2
    assert len(deletes) == 3  # the failed one was attempted, not skipped


def test_reap_tolerates_missing_schtasks(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX hosts have no schtasks at all (FileNotFoundError) — the reap is a
    no-op there, exactly like the leak guard's `_schtasks_jobs`."""

    def _missing(_argv: list[str]) -> None:  # type: ignore[no-untyped-def]
        raise FileNotFoundError("schtasks")

    monkeypatch.setattr(st, "_run", _missing)
    assert st.reap_stale_tasks() == 0
