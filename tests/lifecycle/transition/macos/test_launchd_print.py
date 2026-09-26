"""Supported ``launchctl print`` contract: native captures parse, everything else refuses.

Fixtures are macOS 26.6.2 (25G83) captures of the finite helper job with only
path prefixes normalized; line structure and whitespace are byte-exact.
``killed-30.txt`` / ``killed-31.txt`` are the real finite helper killed by
SIGUSR1 / SIGUSR2; ``pending-spawn.txt`` is the review's probe job whose
program was missing at spawn.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

import pytest

from cli.release_transition import launchd_print
from cli.release_transition.launchd_print import (
    LaunchdFormatError,
    LaunchdPendingSpawnError,
    read_job,
)

_FIXTURES = Path(__file__).with_name("fixtures")
_TARGET = "gui/501/com.ava.review.finite-measure.724be4c7340c.plain"
_FAIL = "gui/501/com.ava.review.finite-measure.cc65579c4b98.fail"
_KILLED = "gui/501/com.ava.review.finite-measure.cc65579c4b98.child"


def _text(name: str) -> str:
    return (_FIXTURES / name).read_text()


def test_running_capture_yields_one_owner_and_exact_definition() -> None:
    job = read_job(_text("running.txt"), _TARGET)
    assert job.state == "running"
    assert (job.pid, job.exit_code, job.signal, job.runs) == (48263, None, None, 1)
    assert (job.uid, job.asid, job.umask, job.exit_timeout) == (501, 100023, "22", 20)
    assert job.properties == ("runatload", "inferred program")
    assert job.program == "/fixture/helper/AvaPermissionsHelper"
    assert job.arguments[:5] == (
        "/fixture/helper/AvaPermissionsHelper",
        "--finite-executor",
        "v1",
        "--cwd",
        "/fixture/job/plain",
    )
    assert job.arguments[-1] == "natural"
    assert job.working_directory == "/fixture/job/plain"
    assert job.stdout_path == "/fixture/job/plain/stdout.log"


@pytest.mark.parametrize(
    ("name", "target", "code", "signal"),
    [
        ("exited-0.txt", _TARGET, 0, None),
        ("exited-80.txt", _FAIL, 80, None),
        ("killed-9.txt", _KILLED, None, 9),
    ],
)
def test_terminal_captures_keep_exit_code_and_signal_distinct(
    name: str, target: str, code: int | None, signal: int | None
) -> None:
    job = read_job(_text(name), target)
    assert (job.state, job.pid, job.runs) == ("not running", None, 1)
    assert (job.exit_code, job.signal) == (code, signal)


@pytest.mark.parametrize(("name", "number"), [("killed-30.txt", 30), ("killed-31.txt", 31)])
def test_native_user_signal_captures_of_the_finite_helper(name: str, number: int) -> None:
    """Review P2-2: "User defined signal 1: 30" contains a digit before the number."""
    text = _text(name)
    job = read_job(text, text.splitlines()[0].removesuffix(" = {"))
    assert (job.state, job.pid, job.runs, job.exit_code, job.signal) == (
        "not running",
        None,
        1,
        None,
        number,
    )
    assert job.arguments[5:7] == ("--group-receipt", job.arguments[6])


def test_nested_coalition_counters_are_never_job_facts() -> None:
    # Both coalitions still say "active count = 1" after the owner exited.
    text = _text("exited-0.txt")
    assert text.count("active count = 1") == 2
    assert read_job(text, _TARGET).state == "not running"


def test_other_job_target_is_refused() -> None:
    with pytest.raises(LaunchdFormatError, match="exactly the requested job"):
        read_job(_text("running.txt"), _TARGET + "x")


def _replace(name: str, old: str, new: str) -> str:
    text = _text(name)
    assert text.count(old) == 1, old
    return text.replace(old, new)


@pytest.mark.parametrize(
    ("name", "old", "new", "reason"),
    [
        # Unknown or ambiguous structure.
        ("running.txt", "\truns = 1\n", "\truns = 1\n\truns = 1\n", "ambiguous field"),
        (
            "running.txt",
            "\tcpumon = default\n",
            "\tcpumon = default\n\tjob state = spawn failed\n",
            "unsupported field",
        ),
        (
            "running.txt",
            "\tcpumon = default\n",
            "\tcpumon = default\n\tendpoints = {\n\t}\n",
            "unexpected block",
        ),
        ("running.txt", "\t\t-I\n", "\t\tnested = {\n", "nested block"),
        ("running.txt", "\tumask = 22\n", "  umask = 22\n", "indentation"),
        ("running.txt", "\tumask = 22\n", "\tumask 22\n", "ambiguous field"),
        ("running.txt", "\tasid = 100023\n", "", "critical job facts"),
        (
            "running.txt",
            "\tprogram = /fixture/helper/AvaPermissionsHelper\n",
            "",
            "critical job facts",
        ),
        # Unknown enumerations and inconsistent owner facts.
        ("running.txt", "\tstate = running\n", "\tstate = spawned\n", "type/state"),
        ("running.txt", "\ttype = LaunchAgent\n", "\ttype = LaunchDaemon\n", "type/state"),
        ("running.txt", "\tpid = 48263\n", "", "running launchd job facts"),
        ("running.txt", "\tpid = 48263\n", "\tpid = 1\n", "no process"),
        ("running.txt", "\tpid = 48263\n", "\tpid = -4\n", "decimal integer"),
        (
            "running.txt",
            "{\n\tactive count = 1\n",
            "{\n\tactive count = 2\n",
            "running launchd job facts",
        ),
        (
            "running.txt",
            "\tlast exit code = (never exited)\n",
            "\tlast exit code = 0\n",
            "running launchd job facts",
        ),
        (
            "running.txt",
            "\tdomain = gui/501 [100023]\n",
            "\tdomain = gui/501 [100024]\n",
            "domain/session",
        ),
        ("running.txt", "\tdomain = gui/501 [100023]\n", "\tdomain = user/501\n", "domain/session"),
        ("exited-0.txt", "\tlast exit code = 0\n", "", "terminal launchd job facts"),
        (
            "exited-0.txt",
            "\tlast exit code = 0\n",
            "\tlast exit code = 0\n\tlast terminating signal = Killed: 9\n",
            "terminal launchd job facts",
        ),
        (
            "exited-0.txt",
            "\tlast exit code = 0\n",
            "\tlast exit code = 0\n\tpid = 5\n",
            "terminal launchd job facts",
        ),
        (
            "exited-0.txt",
            "{\n\tactive count = 0\n",
            "{\n\tactive count = 1\n",
            "terminal launchd job facts",
        ),
        ("exited-0.txt", "\tlast exit code = 0\n", "\tlast exit code = zero\n", "exit code"),
        (
            "killed-9.txt",
            "\tlast terminating signal = Killed: 9\n",
            "\tlast terminating signal = 9\n",
            "terminating signal",
        ),
    ],
)
def test_unknown_or_conflicting_output_refuses(name: str, old: str, new: str, reason: str) -> None:
    target = {"exited-80.txt": _FAIL, "killed-9.txt": _KILLED}.get(name, _TARGET)
    with pytest.raises(LaunchdFormatError, match=reason):
        read_job(_replace(name, old, new), target)


def test_unterminated_block_and_truncated_output_refuse() -> None:
    text = _text("running.txt")
    with pytest.raises(LaunchdFormatError):
        read_job(text.rsplit("}", 1)[0], _TARGET)
    truncated = text.split("\tworking directory", 1)[0] + "}\n"
    with pytest.raises(LaunchdFormatError, match="critical job facts"):
        read_job(truncated, _TARGET)


# Review P2-2: launchd prints Darwin's strsignal(3) text, which may contain "/"
# or digits ("Trace/BPT trap: 5", "User defined signal 1: 30"). Lines are the
# native captures of the review probes (macOS 26.6.2); a crash signal also adds
# "successive crashes".
@pytest.mark.parametrize(
    ("lines", "number"),
    [
        ("\tsuccessive crashes = 1\n\tlast terminating signal = Trace/BPT trap: 5\n", 5),
        ("\tlast terminating signal = User defined signal 1: 30\n", 30),
        ("\tlast terminating signal = User defined signal 2: 31\n", 31),
        ("\tsuccessive crashes = 2\n\tlast terminating signal = Abort trap: 6\n", 6),
    ],
)
def test_native_signal_texts_parse_to_their_signal(lines: str, number: int) -> None:
    text = _replace("killed-9.txt", "\tlast terminating signal = Killed: 9\n", lines)
    job = read_job(text, _KILLED)
    assert (job.state, job.exit_code, job.signal) == ("not running", None, number)


@pytest.mark.parametrize(
    "text",
    [
        "Killed: 10",
        "Killed",
        "User defined signal 1: 31",
        "Unknown signal: 32",
        "Trace/BPT trap: 5 ",
        "killed: 9",
    ],
)
def test_signal_text_outside_the_darwin_table_refuses(text: str) -> None:
    changed = _replace(
        "killed-9.txt",
        "\tlast terminating signal = Killed: 9\n",
        f"\tlast terminating signal = {text}\n",
    )
    with pytest.raises(LaunchdFormatError, match="terminating signal"):
        read_job(changed, _KILLED)


@pytest.mark.skipif(sys.platform != "darwin", reason="compares with this host's strsignal(3)")
def test_signal_table_matches_this_hosts_strsignal() -> None:
    host = {int(number): signal.strsignal(number) for number in signal.valid_signals()}
    assert {
        number: f"{text}: {number}" for number, text in launchd_print._DARWIN_STRSIGNAL.items()
    } == {number: host[number] for number in range(1, 32)}


def test_pending_spawn_is_custody_not_a_format_error() -> None:
    """Review P3-3: a missing program leaves a spawn launchd may still perform."""
    text = _text("pending-spawn.txt")
    target = text.splitlines()[0].removesuffix(" = {")
    with pytest.raises(LaunchdPendingSpawnError, match="pending spawn"):
        read_job(text, target)
    with pytest.raises(LaunchdFormatError, match="exactly the requested job"):
        read_job(text, target + "x")


_DOMAIN = (
    "gui/501 = {\n\ttype = login\n\thandle = 100023\n\tactive count = 474\n"
    "\tsecurity context = {\n\t\tuid = 501\n\t\tasid = 100023\n\t}\n\n"
    "\tservices = {\n\t\t    3621      - \tapplication.fixture\n\t}\n}\n"
)


def test_login_domain_session_is_read_only_from_its_security_context() -> None:
    assert launchd_print.read_domain_asid(_DOMAIN, 501) == 100023
    for bad in (
        _DOMAIN.replace("gui/501 = {", "gui/502 = {"),
        _DOMAIN.replace("\t\tuid = 501\n", "\t\tuid = 0\n"),
        _DOMAIN.replace("\t\tasid = 100023\n", "\t\tasid = x\n"),
        _DOMAIN.replace("\t\tasid = 100023\n", "\t\tasid = 1\n\t\textra = 2\n"),
        _DOMAIN.replace("\tsecurity context = {\n", "\tsecurity context = {\n\t\tflag = 1\n"),
        _DOMAIN.replace("\tservices", "\tsecurity context = {\n\t}\n\tservices"),
        _DOMAIN.rstrip("}\n"),
    ):
        with pytest.raises(LaunchdFormatError):
            launchd_print.read_domain_asid(bad, 501)
