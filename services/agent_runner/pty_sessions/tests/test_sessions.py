"""Sessions of the pty-sessions service: real `bash -l -i` shells on real ptys.

Each test runs a service subprocess under its own tmp `AVA_HOME` (`pty_service`)
and drives it only through `base.sessions.pty.client`, the shape every consumer
uses. POSIX-only (pty.fork + bash).
"""

from __future__ import annotations

import os
import shlex
import signal
import sys
from pathlib import Path

import psutil
import pytest

from base.native_process.os_platform import IS_WINDOWS
from base.sessions.pty import client
from base.sessions.pty.tests.job_wait import wait_for_foreground, wait_for_job
from tests.path_scoped import pty_shells as support
from tests.path_scoped.pty_service import PtyServiceProcess
from tests.path_scoped.pty_service import pty_service as pty_service
from tests.path_scoped.pty_shells import (
    new,
    output_until,
    press,
    screen,
    shell_process,
    type_line,
    wait_for,
)

pytestmark = [
    pytest.mark.skipif(IS_WINDOWS, reason="pty sessions are POSIX-only"),
    pytest.mark.usefixtures("pty_service"),
]


def test_new_send_capture_loop(unit_home: Path) -> None:
    assert new("ava-test-loop-1", unit_home) is True
    assert client.has_session("ava-test-loop-1")
    type_line("ava-test-loop-1", "echo hello-pty")
    assert "hello-pty" in output_until("ava-test-loop-1", "hello-pty")


def test_new_is_idempotent_for_a_live_session(unit_home: Path) -> None:
    name = "ava-test-ido-1"
    assert new(name, unit_home) is True
    (first,) = client.list_sessions()
    assert new(name, unit_home) is False, "a second new accepts the live session"
    (second,) = client.list_sessions()
    assert second == first, "idempotent new must not relaunch"
    type_line(name, "echo still-one-shell")
    output_until(name, "still-one-shell")


def test_the_initial_command_runs_once_the_shell_is_ready(unit_home: Path) -> None:
    new("ava-test-cmd-1", unit_home, cmd="echo initial-command-ran")
    output_until("ava-test-cmd-1", "initial-command-ran")


def test_new_honors_cwd(unit_home: Path) -> None:
    cwd = unit_home / "somewhere"
    cwd.mkdir()
    new("ava-test-cwd-1", cwd)
    type_line("ava-test-cwd-1", "pwd")

    # The pty is 120 columns wide and pytest's tmp path can exceed that, wrapping
    # `pwd`'s output mid-path with a newline the terminal inserted rather than the
    # shell: compare against the capture with the wrap newlines stripped (issue #77).
    assert wait_for(lambda: str(cwd) in screen("ava-test-cwd-1").replace("\n", ""))


def test_the_session_record_carries_identity_and_launch_facts(unit_home: Path) -> None:
    name = "ava-test-rec-1"
    cwd = unit_home / "work"
    cwd.mkdir()
    new(name, cwd)
    (info,) = client.list_sessions()
    assert info.name == name
    assert info.cwd == str(cwd)
    assert info.cmd == "/bin/bash -l -i"
    assert info.started_at > 0
    assert psutil.Process(info.pid).is_running()
    assert info.record().pid == info.pid


def test_list_filters_by_prefix_and_has_reflects_liveness(unit_home: Path) -> None:
    for name in ("ava-test-a-1", "ava-test-a-2", "ava-test-b-1"):
        new(name, unit_home)
    assert [s.name for s in client.list_sessions("ava-test-a-")] == ["ava-test-a-1", "ava-test-a-2"]
    assert sorted(client.live_sessions()) == ["ava-test-a-1", "ava-test-a-2", "ava-test-b-1"]
    assert client.has_session("ava-test-b-1")
    assert not client.has_session("ava-test-c-1")


def test_send_transports_tricky_text(unit_home: Path) -> None:
    name = "ava-test-b64-1"
    new(name, unit_home)
    wide = chr(0x4F60) + chr(0x597D)  # multi-byte UTF-8 must survive the wire and the shell
    tricky = f'quote " and spaces {wide}'
    type_line(name, f"echo '{tricky}'")
    assert tricky in output_until(name, tricky)


def test_send_without_enter_does_not_submit(unit_home: Path) -> None:
    """Text and Enter are separate writes (the SDK contract); text alone must sit
    unsubmitted on the line editor."""
    name = "ava-test-noent-1"
    new(name, unit_home)
    type_line(name, "echo ready-check")
    output_until(name, "ready-check")
    client.send(name, b"echo not-submitted-yet")

    # Whitespace-normalized: with a long cwd the echoed text can wrap mid-word.
    assert wait_for(lambda: "echonot-submitted-yet" in "".join(screen(name).split()))
    assert "not-submitted-yet" not in [line.strip() for line in screen(name).split("\n")]
    press(name, "Enter")
    output_until(name, "not-submitted-yet")


def test_send_keys_ctrl_c_interrupts_the_foreground(unit_home: Path) -> None:
    name = "ava-test-cc-1"
    new(name, unit_home)
    type_line(name, "cat")
    shell = shell_process(name)
    job = wait_for_job(shell, ["cat"])
    wait_for_foreground(job)
    press(name, "C-c")
    wait_for_foreground(shell)
    type_line(name, "echo after-interrupt")
    output_until(name, "after-interrupt")
    assert client.has_session(name)


def test_shell_foreground_wait_does_not_accept_an_interrupted_live_job(
    unit_home: Path,
) -> None:
    """A controlled SIGINT handler keeps the job foreground until explicitly released."""
    ready, interrupted, consumed, release = (
        unit_home / item for item in ("ready", "interrupted", "consumed", "release")
    )
    code = (
        "import pathlib,signal,sys,time\n"
        "def interrupt(*_):\n"
        f"    pathlib.Path({str(interrupted)!r}).touch()\n"
        f"    target = pathlib.Path({str(consumed)!r})\n"
        "    temporary = target.with_suffix('.tmp')\n"
        "    temporary.write_text(sys.stdin.readline())\n"
        "    temporary.replace(target)\n"
        f"    while not pathlib.Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGINT,interrupt)\n"
        f"pathlib.Path({str(ready)!r}).touch()\n"
        "while True: time.sleep(1)\n"
    )
    name = "ava-test-foreground-sync-1"
    new(name, unit_home)
    shell = shell_process(name)
    type_line(name, shlex.join([sys.executable, "-u", "-c", code]))
    assert wait_for(ready.exists)
    try:
        press(name, "C-c")
        assert wait_for(interrupted.exists)
        # Reproduce the old order: the next line still belongs to the job,
        # so the shell cannot execute it even though SIGINT was delivered.
        type_line(name, "echo premature-input")
        assert wait_for(consumed.exists)
        assert consumed.read_text() == "echo premature-input\n"
        with pytest.raises(AssertionError, match="never gained terminal foreground"):
            wait_for_foreground(shell, timeout=0.1)
        release.touch()
        wait_for_foreground(shell)
        type_line(name, "echo synchronized-input")
        output_until(name, "synchronized-input")
        assert "premature-input" not in [line.strip() for line in screen(name).splitlines()]
    finally:
        release.touch()


def test_send_keys_up_arrow_recalls_history(unit_home: Path) -> None:
    name = "ava-test-up-1"
    new(name, unit_home)
    type_line(name, "echo arrow-marker-77")
    output_until(name, "arrow-marker-77")
    press(name, "Up", "Enter")
    assert wait_for(lambda: screen(name).count("arrow-marker-77") >= 3)


def test_resize_is_seen_by_stty(unit_home: Path) -> None:
    name = "ava-test-resize-1"
    new(name, unit_home)
    type_line(name, "echo stty-ready")
    output_until(name, "stty-ready")
    client.resize(name, 100, 25)
    type_line(name, "stty size")
    output_until(name, "25 100")


def test_capture_visible_screen_renders_tui_layout(unit_home: Path) -> None:
    """A cursor-addressed full-screen program: the visible screen shows the final
    layout (pyte) and the scrollback still holds the history."""
    name = "ava-test-tui-1"
    new(name, unit_home)
    type_line(name, "echo tui-ready")
    output_until(name, "tui-ready")
    type_line(name, "seq 1 45")
    output_until(name, "45")
    type_line(
        name,
        "python3 -c 'import sys; sys.stdout.write(\"\\x1b[2J\\x1b[HFAKE-TOP"
        "\\x1b[5;3Hmid-cell\\x1b[8;1Hbottom-row\")'",
    )
    assert wait_for(lambda: screen(name, scrollback=False).split("\n")[0].startswith("FAKE-TOP"))
    rows = screen(name, scrollback=False).split("\n")
    assert rows[4].startswith("  mid-cell"), rows[:6]
    assert rows[7].startswith("bottom-row"), rows
    assert "\x1b" not in "\n".join(rows), "no raw escape sequences may leak"
    full = screen(name)
    assert "FAKE-TOP" in full and "45" in full, "history must survive the TUI"


def test_capture_lines_caps_the_render(unit_home: Path) -> None:
    name = "ava-test-lines-1"
    new(name, unit_home)
    type_line(name, "echo bulk-line-0")
    output_until(name, "bulk-line-0")
    type_line(name, "seq 1 45")
    output_until(name, "45")
    tail = client.capture(name, 5, scrollback=True)
    assert len(tail.splitlines()) <= 5
    assert "45" in tail and "bulk-line-0" not in tail
    assert "bulk-line-0" in client.capture(name, 200, scrollback=True)


def test_capture_rejects_a_window_outside_the_protective_bounds(unit_home: Path) -> None:
    new("ava-test-bounds-1", unit_home)
    with pytest.raises(ValueError, match="out of range"):
        client.capture("ava-test-bounds-1", 0, scrollback=True)
    with pytest.raises(client.ServiceError) as refused:
        client.request("capture", name="ava-test-bounds-1", lines=10**9)
    assert refused.value.code == 2


def test_the_caller_env_reaches_the_shell(unit_home: Path) -> None:
    name = "ava-test-env-1"
    new(name, unit_home, env={"PTY_TEST_VAR": "hello-from-env", "EMPTY": ""})
    type_line(name, "echo value=$PTY_TEST_VAR")
    output_until(name, "value=hello-from-env")


def test_the_service_profile_marker_and_virtualenv_do_not_leak_into_a_shell(
    pty_service: PtyServiceProcess, unit_home: Path
) -> None:
    """A service launched under a profile marker must not hand it to its shells: a
    leaked marker kills every watcher at `import ava` (Task #856). The service
    environment is otherwise the shell's base, and the creator's overlay wins,
    for the marker and for VIRTUAL_ENV alike."""
    pty_service.stop()
    marked = PtyServiceProcess(
        unit_home,
        {
            "AVA_PROCESS_PROFILE": "runner",
            "VIRTUAL_ENV": "/not/a/venv",
            "PTY_BASE_MARKER": "from-the-service-env",
        },
    )
    marked.start()
    try:
        name = "ava-test-profile-1"
        new(name, unit_home, env={"EXPLICIT": "overlay-wins"})
        type_line(name, "env | grep -E '^(AVA_PROCESS_PROFILE|VIRTUAL_ENV)=' ; echo ENVCHECK_DONE")
        out = output_until(name, "ENVCHECK_DONE")
        assert "AVA_PROCESS_PROFILE=" not in out, out
        assert "VIRTUAL_ENV=" not in out, out
        type_line(name, "echo base=$PTY_BASE_MARKER explicit=$EXPLICIT")
        output_until(name, "base=from-the-service-env explicit=overlay-wins")
        overlaid = "ava-test-profile-2"
        new(overlaid, unit_home, env={"AVA_PROCESS_PROFILE": "gateway"})
        type_line(overlaid, "echo marker=$AVA_PROCESS_PROFILE")
        output_until(overlaid, "marker=gateway")
    finally:
        client.close_all(grace_s=0.5, kill_s=2.0)
        marked.stop()


def test_a_shell_inherits_no_descriptor_of_the_service(unit_home: Path) -> None:
    """The service holds every master; a shell that kept one would hold another
    session's terminal open past its death and defeat the hangup. Only the
    standard descriptors (and `ls`'s own directory handle) may be open in a job."""
    for name in ("ava-test-fd-1", "ava-test-fd-2", "ava-test-fd-3"):
        new(name, unit_home)
    type_line(
        "ava-test-fd-3", 'python3 -c \'import os; print("fds", sorted(os.listdir("/dev/fd")))\''
    )
    out = output_until("ava-test-fd-3", "fds ['0', '1', '2', '3']")
    assert "fds ['0', '1', '2', '3']" in out


def test_a_session_ends_with_its_shell(unit_home: Path) -> None:
    name = "ava-test-exit-1"
    new(name, unit_home)
    type_line(name, "exit")
    assert wait_for(lambda: not client.has_session(name)), "session must die with its shell"
    assert name not in client.live_sessions()
    assert not (unit_home / "run" / "pty").exists(), "no per-session record directory"


def test_ops_after_death_report_no_such_session(unit_home: Path) -> None:
    name = "ava-test-dead-1"
    new(name, unit_home)
    type_line(name, "exit")
    assert wait_for(lambda: not client.has_session(name))
    for call in (
        lambda: client.send(name, b"x"),
        lambda: client.capture(name, 10, scrollback=True),
        lambda: client.resize(name, 80, 24),
    ):
        with pytest.raises(client.ServiceError) as refused:
            call()
        assert refused.value.code == 3


def test_the_transcript_is_written_beside_the_logs(unit_home: Path) -> None:
    name = "ava-test-log-1"
    new(name, unit_home)
    type_line(name, "echo transcript-line")
    output_until(name, "transcript-line")
    transcript = unit_home / "logs" / f"{name}.out.log"
    assert wait_for(lambda: "transcript-line" in transcript.read_text(errors="replace"))


def test_kill_reaps_the_process_tree_without_orphans(unit_home: Path) -> None:
    name = "ava-test-killtree-1"
    new(name, unit_home)
    shell = shell_process(name)
    type_line(name, "sleep 300")
    sleeper = wait_for_job(shell, ["sleep", "300"])
    verdict = client.kill(name, graceful=False)
    assert verdict.interrupted is True
    assert wait_for(lambda: support.gone(shell)), "shell survived the kill"
    assert wait_for(lambda: support.gone(sleeper)), "sleep orphaned by the kill"
    assert not client.has_session(name)


def test_kill_graceful_then_force(unit_home: Path) -> None:
    name = "ava-test-killg-1"
    new(name, unit_home)
    verdict = client.kill(name, graceful=True)
    assert verdict.mode in ("graceful", "forced")
    assert wait_for(lambda: not client.has_session(name))


def test_kill_is_an_idempotent_noop_on_an_absent_session() -> None:
    verdict = client.kill("ava-test-absent-1", graceful=False)
    assert verdict == client.KillVerdict("noop", False)


def test_kill_then_an_immediate_same_name_new_is_a_real_session(unit_home: Path) -> None:
    """kill x then new x with no pause: the new builds a REAL fresh session, never
    adopts the dying one as its success."""
    name = "ava-test-rebirth-1"
    new(name, unit_home)
    (first,) = client.list_sessions()
    client.kill(name, graceful=False)
    assert new(name, unit_home) is True
    (second,) = client.list_sessions()
    assert second.pid != first.pid, "must be a fresh shell, not the dying one"
    type_line(name, "echo reborn-ok")
    output_until(name, "reborn-ok")


def test_kill_idle_reports_not_interrupted(unit_home: Path) -> None:
    name = "ava-test-verdict-idle-1"
    new(name, unit_home)
    type_line(name, "echo verdict-idle-ready")
    output_until(name, "verdict-idle-ready")
    assert client.kill(name, graceful=False).interrupted is False
    assert not client.has_session(name)


@pytest.mark.parametrize(("job", "interrupted"), [("sleep 300", True), ("sleep 300 &", False)])
def test_kill_verdict_reports_foreground_work(unit_home: Path, job: str, interrupted: bool) -> None:
    """The verdict describes known foreground work, not all background work."""
    name = "ava-test-verdict-job-1"
    new(name, unit_home)
    type_line(name, "echo verdict-job-ready")
    output_until(name, "verdict-job-ready")
    type_line(name, job)
    process = wait_for_job(shell_process(name), ["sleep", "300"])
    if interrupted:
        wait_for_foreground(process)
    else:
        assert wait_for(lambda: screen(name).rstrip().endswith(("$", "#")))
    assert client.kill(name, graceful=False).interrupted is interrupted
    assert not client.has_session(name)


def test_a_zombie_shell_is_not_a_live_session(unit_home: Path) -> None:
    """A shell that exited but is not yet reaped cannot execute: `has` says no
    even before the reader gets to reap it."""
    name = "ava-test-zombie-1"
    new(name, unit_home)
    shell = shell_process(name)
    os.kill(shell.pid, signal.SIGSTOP)  # the reader's reap needs the shell dead, not stopped
    os.kill(shell.pid, signal.SIGKILL)
    assert wait_for(lambda: not client.has_session(name))
    assert name not in client.live_sessions()


def test_a_process_inside_a_session_is_told_it_is_hosted_there(unit_home: Path) -> None:
    """A stop or restart closes every persistent terminal, so the host-transition verbs
    refuse to run from inside one (`hosting_supervised_session`): they would end
    themselves mid-flight."""
    import sys

    from tests.path_scoped.pty_service import _REPO

    name = "ava-test-hosting-1"
    new(name, _REPO, {"AVA_HOME": str(unit_home), "AVA_CONFIG_FETCH": "skip"})
    probe = (
        "from base.host.proc import hosting_supervised_session as hosting; "
        "print('hosted-in=' + str(hosting()))"
    )
    type_line(name, f'{sys.executable} -c "{probe}"')
    output_until(name, f"hosted-in={name}")
    assert wait_for(lambda: hosting_from_outside() is None)


def hosting_from_outside() -> str | None:
    """The same question asked by this test process, which no session hosts."""
    from base.host.proc import hosting_supervised_session

    return hosting_supervised_session()


def test_initial_command_provenance_is_opt_in_and_keeps_original_allocation(
    unit_home: Path,
) -> None:
    name = "ava-test-command-provenance"
    cmd = "echo first-allocation"
    assert new(name, unit_home, cmd=cmd)
    default = client.request("list", prefix=name)["sessions"]
    assert "initial_command" not in default[0]
    assert client.list_sessions(name)[0].initial_command is None
    first = client.list_sessions(name, include_initial_command=True)[0]
    assert first.initial_command == cmd
    assert not new(name, unit_home, cmd="echo replacement")
    second = client.list_sessions(name, include_initial_command=True)[0]
    assert second.initial_command == cmd
    assert second.started_at == first.started_at


def test_list_rejects_invalid_initial_command_metadata_flag() -> None:
    with pytest.raises(client.ServiceError, match="include_initial_command must be a boolean"):
        client.request("list", include_initial_command="yes")
