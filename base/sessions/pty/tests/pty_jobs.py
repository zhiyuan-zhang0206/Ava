from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

import psutil

from base.sessions.pty.tests import pty_shells as support

TERM_OK = "import time\nprint('job-ready', flush=True)\nwhile True: time.sleep(0.1)\n"

STUBBORN = (
    "import os,signal,time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
    "print('stubborn-ready', flush=True)\n"
    f"while True: time.sleep(0.1); os.kill({os.getpid()}, 0)\n"
)

LOOP_SHELL = "bash -c 'while true; do sleep 1; done'"

FORK_ON_TERM = (
    "import os,signal,sys,time\n"
    "def on_term(*_):\n"
    "    if os.fork() == 0:\n"
    "        open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
    "        os.rename(sys.argv[1] + '.tmp', sys.argv[1])\n"
    "        signal.signal(signal.SIGTERM, signal.{disposition})\n"
    "        signal.signal(signal.SIGHUP, signal.{disposition})\n"
    "        while True: time.sleep(0.1)\n"
    "    os._exit(0)\n"
    "signal.signal(signal.SIGTERM, on_term)\n"
    "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
    "open(sys.argv[1] + '.ready', 'w').close()\n"
    "while True: time.sleep(0.1)\n"
)

FORK_CHAIN = (
    "import os,signal,sys,time\n"
    "def on_term(*_):\n"
    "    if os.fork() == 0:\n"
    "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "        signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
    "        for _ in range({hops}):\n"
    "            time.sleep({hop_ms} / 1000.0)\n"
    "            if os.fork() != 0:\n"
    "                os._exit(0)\n"
    "        open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
    "        os.rename(sys.argv[1] + '.tmp', sys.argv[1])\n"
    "        while True: time.sleep(0.1)\n"
    "    os._exit(0)\n"
    "signal.signal(signal.SIGTERM, on_term)\n"
    "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
    "open(sys.argv[1] + '.ready', 'w').close()\n"
    "while True: time.sleep(0.1)\n"
)


def double_forked(pidfile: Path, *, ignore: tuple[str, ...]) -> str:
    ignored = "".join(f"        signal.signal(signal.{sig}, signal.SIG_IGN)\n" for sig in ignore)
    staged = f"{pidfile}.tmp"
    return (
        "import os,signal,time\n"
        "if os.fork() == 0:\n"
        "    if os.fork() == 0:\n"
        f"{ignored}"
        f"        open({staged!r}, 'w').write(str(os.getpid()))\n"
        f"        os.rename({staged!r}, {str(pidfile)!r})\n"
        "        while True: time.sleep(0.1)\n"
        "    os._exit(0)\n"
        "os.wait()\n"
    )


def setsid_child(pidfile: Path, *, escape: bool) -> str:
    staged = f"{pidfile}.tmp"
    parent_tail = "os._exit(0)\n" if escape else "os.wait()\n"
    return (
        "import os,signal,time\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        f"    open({staged!r}, 'w').write(str(os.getpid()))\n"
        f"    os.rename({staged!r}, {str(pidfile)!r})\n"
        "    while True: time.sleep(0.1)\n"
        "time.sleep(0.5)\n" + parent_tail
    )


def create(name: str, home: Path, source: str, *, args: str = "") -> psutil.Process:
    script = home / f"{name}.job.py"
    script.write_text(source, encoding="utf-8")
    assert support.new(name, home, {"AVA_HOME": str(home)}, f"python3 -u {script} {args}".strip())
    return support.shell_process(name)


def start(name: str, home: Path, source: str, *, args: str = "") -> psutil.Process:
    shell = create(name, home, source, args=args)
    deadline = time.monotonic() + support.WAIT_S
    while time.monotonic() < deadline:
        if live_children(shell):
            return shell
        time.sleep(0.05)
    raise AssertionError(f"the job of {name} never started")


def live_children(shell: psutil.Process) -> list[psutil.Process]:
    with contextlib.suppress(psutil.NoSuchProcess, psutil.ZombieProcess):
        return [child for child in shell.children(recursive=True) if not support.gone(child)]
    return []


def wait_for_file(path: Path, what: str) -> int:
    assert support.wait_for(path.exists, 15), f"{what} never appeared"
    return int(path.read_text(encoding="utf-8"))


def wait_exit(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if support.gone(psutil.Process(pid)):
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


def kill_quietly(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, 9)
