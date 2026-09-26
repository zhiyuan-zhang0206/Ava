"""Supported ``launchctl print`` contract: native captures parse, everything else refuses.

Fixtures are macOS 26.6.2 (25G83) captures of the finite helper job with only
path prefixes normalized; line structure and whitespace are byte-exact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.release_transition.launchd_print import LaunchdFormatError, read_job

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
        ("running.txt", "\tstate = running\n", "\tstate = spawn scheduled\n", "type/state"),
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
