"""What a session survives, and what a dead service leaves behind.

The one property the service exists to keep: a session outlives the processes that
use it, an agent host included. The service itself is an ordinary roster process: its
stop closes its sessions, and a crash leaves only what the master's hangup could not
reach, which the ledger lets the next start sweep.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
import psycopg
import pytest

from base.db import create_agent
from base.native_process.os_platform import IS_WINDOWS
from base.native_process.ownership import OwnedProcess
from base.sessions.pty import client, closure
from base.sessions.pty.paths import ledger_path
from services.pty_sessions import ledger
from tests.path_scoped import pty_jobs as jobs
from tests.path_scoped import pty_shells as support
from tests.path_scoped.pty_service import _REPO, PtyServiceProcess
from tests.path_scoped.pty_service import pty_service as pty_service
from tests.path_scoped.pty_shells import new, output_until, type_line, wait_for

pytestmark = [
    pytest.mark.skipif(IS_WINDOWS, reason="pty sessions are POSIX-only"),
]

# A stand-in for the agent host: a separate process that creates a session through the
# client, reports it, and stays alive until it is killed.
_AGENT_HOST = """
import sys, time
from base.sessions.pty import client
client.create_session(sys.argv[1], sys.argv[2], {}, sys.argv[3])
print("created", flush=True)
while True:
    time.sleep(1)
"""

_COUNTER_JOB = (
    "import sys, time\n"
    "n = 0\n"
    "while True:\n"
    "    n += 1\n"
    "    open(sys.argv[1], 'w').write(str(n))\n"
    "    time.sleep(0.1)\n"
)


@pytest.mark.usefixtures("pty_service")
def test_a_session_outlives_the_process_that_created_it(unit_home: Path) -> None:
    """Kill the creating process outright (an agent host restarting): the session is
    still there, its job still runs, and the next client's call succeeds."""
    name = "ava-agent-987-shell-1-survivor"
    counter = unit_home / "count"
    script = unit_home / "counter.py"
    script.write_text(_COUNTER_JOB, encoding="utf-8")
    host = subprocess.Popen(  # noqa: S603 — repo-internal interpreter + inline program
        [
            sys.executable,
            "-c",
            _AGENT_HOST,
            name,
            str(unit_home),
            f"python3 -u {script} {counter} &",
        ],
        cwd=_REPO,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert host.stdout is not None and host.stdout.readline().strip() == "created"
    assert wait_for(counter.exists)
    before = int(counter.read_text())

    host.send_signal(signal.SIGKILL)
    host.wait(timeout=10)

    assert client.has_session(name), "the session died with its creator"
    assert wait_for(lambda: int(counter.read_text()) > before + 3), "its job stopped running"
    type_line(name, "echo reached-by-the-next-client")
    output_until(name, "reached-by-the-next-client")
    assert client.kill(name, graceful=False).interrupted is True


# The same stand-in, through the backend every SDK call goes through.
_BACKEND_HOST = """
import sys, time
from pathlib import Path
from base.sessions.backend import get_shell_backend
assert get_shell_backend().new_session(sys.argv[1], "", Path(sys.argv[2]), env={})
print("created", flush=True)
while True:
    time.sleep(1)
"""


@pytest.mark.usefixtures("pty_service")
def test_a_backend_session_outlives_the_process_that_created_it(unit_home: Path) -> None:
    """The SDK's transport: kill the process that created a session through
    `get_shell_backend()` (an agent host going down) and the next backend call in a new
    process succeeds against the same shell."""
    from base.sessions.backend import PtySessionBackend

    name = "ava-agent-987-shell-2-backend"
    host = subprocess.Popen(  # noqa: S603 — repo-internal interpreter + inline program
        [sys.executable, "-c", _BACKEND_HOST, name, str(unit_home)],
        cwd=_REPO,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert host.stdout is not None and host.stdout.readline().strip() == "created"
    (before,) = client.list_sessions()

    host.send_signal(signal.SIGKILL)
    host.wait(timeout=10)

    backend = PtySessionBackend()
    assert backend.has_session(name)
    backend.send(name, "echo reached-through-the-backend")
    backend.send_keys(name, "Enter")
    output_until(name, "reached-through-the-backend")
    assert [s.pid for s in client.list_sessions()] == [before.pid], "the same shell answered"
    assert backend.kill_session(name) == (True, "forced")


@pytest.mark.usefixtures("pty_service")
def test_a_session_outlives_every_client_restarting_in_turn(unit_home: Path) -> None:
    name = "ava-agent-987-shell-1-reconnect"
    new(name, unit_home)
    (first,) = client.list_sessions()
    for generation in range(3):
        result = subprocess.run(  # noqa: S603 — repo-internal interpreter + inline program
            [
                sys.executable,
                "-c",
                "from base.sessions.pty import client; "
                f"client.send({name!r}, b'echo generation-{generation}\\r')",
            ],
            cwd=_REPO,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        output_until(name, f"generation-{generation}")
    assert [s.pid for s in client.list_sessions()] == [first.pid]


def test_stopping_the_service_closes_its_sessions_and_removes_the_socket(
    pty_service: PtyServiceProcess, unit_home: Path
) -> None:
    """SIGTERM is the roster stop: the service closes what is still alive (a job that
    ignores HUP and TERM is SIGKILLed) and exits clean, leaving no socket."""
    shell = jobs.start("ava-agent-987-shell-3-stop", unit_home, jobs.STUBBORN)
    (job,) = jobs.live_children(shell)

    pty_service.signal(signal.SIGTERM)

    assert pty_service.wait() == 0
    assert jobs.wait_exit(job.pid), "the service left a job running behind its stop"
    assert jobs.wait_exit(shell.pid)
    from base.sessions.pty.paths import service_socket_path

    assert not service_socket_path().exists()
    assert client.list_sessions() == []


def test_a_crashed_service_leaves_nothing_the_next_start_cannot_sweep(
    pty_service: PtyServiceProcess, unit_home: Path
) -> None:
    """SIGKILL the service. The kernel hangs up every shell, which ends the ones that honour
    it; what survives is a job that ignores the hangup under a dead shell, and a shell that
    ignores it too. The ledger names both, so the next start closes them before it serves."""
    jobs_shell = jobs.start("ava-agent-987-shell-4-orphan", unit_home, jobs.STUBBORN)
    (orphan_job,) = jobs.live_children(jobs_shell)
    script = unit_home / "deaf.job.py"
    script.write_text(jobs.STUBBORN, encoding="utf-8")
    new("ava-agent-987-shell-5-deaf", unit_home, cmd=f"trap '' HUP; python3 -u {script}")
    deaf_shell = support.shell_process("ava-agent-987-shell-5-deaf")
    assert wait_for(lambda: bool(jobs.live_children(deaf_shell)))
    (deaf_job,) = jobs.live_children(deaf_shell)

    def ledger_knows_both_jobs() -> bool:
        sessions = json.loads(ledger_path().read_text())["sessions"]
        return all(
            sessions.get(name, {}).get("members")
            for name in ("ava-agent-987-shell-4-orphan", "ava-agent-987-shell-5-deaf")
        )

    assert wait_for(ledger_knows_both_jobs, timeout=ledger.SNAPSHOT_INTERVAL_S * 3)

    pty_service.signal(signal.SIGKILL)
    pty_service.wait()
    assert wait_for(lambda: jobs.wait_exit(jobs_shell.pid, 0.1)), (
        "the hangup must end an honest shell"
    )
    assert psutil.pid_exists(orphan_job.pid), "precondition: a job that ignores the hangup survives"
    assert psutil.pid_exists(deaf_job.pid), "precondition: so does a shell that ignores it"

    pty_service.start()  # the start sweeps before it answers its first ping

    assert jobs.wait_exit(orphan_job.pid, timeout=10), "the orphaned job outlived the sweep"
    assert jobs.wait_exit(deaf_job.pid, timeout=10)
    assert jobs.wait_exit(deaf_shell.pid, timeout=10)
    assert client.list_sessions() == []
    assert "pty sweep: closing 2 session(s)" in pty_service.output()
    assert json.loads(ledger_path().read_text())["sessions"] == {}, "the sweep clears the ledger"


@pytest.mark.usefixtures("pty_service")
def test_a_crashed_services_stale_socket_is_replaced_by_the_next_start(
    pty_service: PtyServiceProcess, unit_home: Path
) -> None:
    new("ava-agent-987-shell-6-stale", unit_home)
    pty_service.signal(signal.SIGKILL)
    pty_service.wait()

    pty_service.start()

    assert client.request("ping")["pid"] == pty_service.pid
    assert not client.has_session("ava-agent-987-shell-6-stale")


def test_the_sweep_closes_the_members_a_dead_shell_left_and_reports_the_busy_session(
    tmp_path: Path,
) -> None:
    """The ledger sweep in isolation: a session leader killed with a job behind it that
    ignores the hangup. The recorded job proves the session id, so it is closed with its
    whole tree, and the outcome names the busy session for its owner."""
    path = tmp_path / "ledger.json"
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os, signal, subprocess, sys, time\n"
            "job = subprocess.Popen([sys.executable, '-c', 'import signal, time\\n"
            "signal.signal(signal.SIGHUP, signal.SIG_IGN)\\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
            "time.sleep(300)'])\n"
            "print(job.pid, flush=True)\n"
            "time.sleep(300)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert leader.stdout is not None
    job_pid = int(leader.stdout.readline())
    shell = OwnedProcess.capture(psutil.Process(leader.pid))
    job = OwnedProcess.capture(psutil.Process(job_pid))
    ledger.write(path, [closure.Target("ava-agent-1-shell-1-swept", shell, (job,))])
    leader.kill()
    leader.wait(timeout=10)
    assert psutil.pid_exists(job_pid), "precondition: the job outlives its session leader"

    started = time.monotonic()
    outcome = ledger.sweep(path)

    assert time.monotonic() - started < ledger.SWEEP_HANGUP_WAIT_S + 3 * ledger.SWEEP_KILL_S
    assert jobs.wait_exit(job_pid, timeout=5), "the sweep left the job running"
    assert [closed.name for closed in outcome.closed] == ["ava-agent-1-shell-1-swept"]
    assert outcome.survivors == ()
    assert ledger.read(path) == [], "the ledger is cleared once swept"


def test_the_sweep_never_signals_a_pid_that_is_no_longer_the_recorded_process(
    tmp_path: Path,
) -> None:
    bystander = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        real = OwnedProcess.capture(psutil.Process(bystander.pid))
        stale = OwnedProcess(
            real.pid, real.birth - 1000.0, None if real.starttime is None else real.starttime - 1
        )
        path = tmp_path / "ledger.json"
        ledger.write(path, [closure.Target("ava-agent-1-shell-2-recycled", stale, ())])

        outcome = ledger.sweep(path)

        assert outcome == closure.Outcome()
        assert bystander.poll() is None, "a recycled pid was signalled"
    finally:
        bystander.kill()
        bystander.wait(timeout=10)


def test_an_unreadable_ledger_sweeps_nothing(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text("{not json")
    assert ledger.sweep(path) == closure.Outcome()
    assert json.loads(path.read_text())["sessions"] == {}
    assert path.stat().st_mode & 0o777 == 0o600


def _exited_process() -> OwnedProcess:
    """The identity of a process that has already exited and been reaped."""
    child = subprocess.Popen(["true"])
    identity = OwnedProcess.capture(psutil.Process(child.pid))
    child.wait(timeout=10)
    return identity


def test_the_sweep_reports_a_busy_session_the_crash_ended_whole(tmp_path: Path) -> None:
    """The hangup (or a reboot) ended every process of a session the ledger last saw
    running a job: nothing is left to close, but its owner still lost that job, so the
    session is reported; a session that was only an idle shell is not."""
    path = tmp_path / "ledger.json"
    busy_shell, busy_job, idle_shell = _exited_process(), _exited_process(), _exited_process()
    ledger.write(
        path,
        [
            closure.Target("ava-agent-1-shell-3-busy", busy_shell, (busy_shell, busy_job)),
            closure.Target("ava-agent-1-shell-4-idle", idle_shell, (idle_shell,)),
        ],
    )

    outcome = ledger.sweep(path)

    assert [closed.name for closed in outcome.closed] == ["ava-agent-1-shell-3-busy"]
    assert outcome.closed[0].shell == busy_shell
    assert outcome.survivors == ()
    assert ledger.read(path) == []


def test_the_sweep_never_reports_a_busy_session_whose_shell_is_still_running(
    tmp_path: Path,
) -> None:
    """A recorded shell that is still its recorded process is closed by the closure, not
    reported twice; a recycled pid under the shell's name is not that process."""
    path = tmp_path / "ledger.json"
    bystander = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        real = OwnedProcess.capture(psutil.Process(bystander.pid))
        stale = OwnedProcess(
            real.pid, real.birth - 1000.0, None if real.starttime is None else real.starttime - 1
        )
        ledger.write(path, [closure.Target("ava-agent-1-shell-5-recycled", stale, (stale, real))])

        outcome = ledger.sweep(path)

        assert [closed.name for closed in outcome.closed] == ["ava-agent-1-shell-5-recycled"]
    finally:
        bystander.kill()
        bystander.wait(timeout=10)


def test_a_crashed_service_tells_the_owner_of_a_busy_session_at_the_next_start(
    pty_service: PtyServiceProcess, unit_home: Path, db_conn: psycopg.Connection, db_url: str
) -> None:
    """SIGKILL the service while a session runs a job: the hangup ends everything, so the
    sweep has nothing to close, yet the next start hands the busy session to the one-shot
    child, which leaves its owner one system inbound."""
    owner = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (owner,),
        )
    db_conn.commit()
    # The unit's `.env` declares what the child needs: its name and its database.
    (unit_home / ".env").write_text(
        f"AVA_MACHINE_NAME=crash-box\nAVA_DB_URL={db_url}\nAVA_REDIS_URL={os.environ['AVA_REDIS_URL']}\n",
        encoding="utf-8",
    )
    name = f"ava-agent-{owner}-shell-7-crashed"
    shell = jobs.start(name, unit_home, jobs.TERM_OK)

    def ledger_knows_the_job() -> bool:
        return bool(json.loads(ledger_path().read_text())["sessions"].get(name, {}).get("members"))

    assert wait_for(ledger_knows_the_job, timeout=ledger.SNAPSHOT_INTERVAL_S * 3)
    pty_service.signal(signal.SIGKILL)
    pty_service.wait()
    assert jobs.wait_exit(shell.pid, timeout=10), "precondition: the hangup ends the shell"

    pty_service.start()

    def inbounds() -> list[tuple[str, str]]:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT source, content FROM inbound_messages WHERE agent_id = %s", (owner,)
            )
            rows = cur.fetchall()
        db_conn.commit()
        return [(str(source), str(content)) for source, content in rows]

    assert wait_for(lambda: bool(inbounds()), timeout=20), pty_service.output()
    ((source, content),) = inbounds()
    assert source == "system" and name in content and "ending uncleanly" in content


def test_an_unreachable_database_costs_the_start_a_log_line_and_nothing_else(
    pty_service: PtyServiceProcess, unit_home: Path
) -> None:
    """The crash notices are a side channel: with the database refusing connections the
    service still starts, answers and serves, and the log says the notices were lost."""
    (unit_home / ".env").write_text(
        "AVA_MACHINE_NAME=crash-box\nAVA_DB_URL=postgresql://nobody:nothing@127.0.0.1:1/none\n",
        encoding="utf-8",
    )
    name = "ava-agent-987-shell-8-lost"
    jobs.start(name, unit_home, jobs.TERM_OK)
    assert wait_for(
        lambda: bool(
            json.loads(ledger_path().read_text())["sessions"].get(name, {}).get("members")
        ),
        timeout=ledger.SNAPSHOT_INTERVAL_S * 3,
    )
    pty_service.signal(signal.SIGKILL)
    pty_service.wait()

    pty_service.start()

    assert client.request("ping")["pid"] == pty_service.pid
    assert wait_for(lambda: "were not all written" in pty_service.output(), timeout=20)
    new("ava-agent-987-shell-9-after", unit_home)
    assert client.has_session("ava-agent-987-shell-9-after")
