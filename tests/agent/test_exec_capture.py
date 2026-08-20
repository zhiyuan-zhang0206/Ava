"""Per-context exec output capture — the hosted-mode isolation lock.

Prerequisite 1 of `future/infra/agent-runner-as-server.md` Phase 1: exec used
to capture agent output by assigning the process-global `sys.stdout` /
`sys.stderr`. Correct with one agent per process, wrong the moment two agents
share one — their concurrent execs would capture each other's prints.

What is locked here:

- an unbound context writes through to the real stream (process mode is
  unchanged, and framework code never lands in an agent's output);
- two concurrent captures each keep their own output (the hosted invariant);
- the capture reaches the worker thread and anything it spawns through
  `asyncio.to_thread`, because the binding rides `contextvars`;
- an abandoned binding (the orphaned-thread case) never touches the process's
  own streams.
"""

import asyncio
import contextvars
import io
import sys
import threading

from agent.graph._exec_capture import (
    _CaptureRouter,
    capture_output,
    current_capture,
    install_capture_routers,
)


def test_install_is_idempotent_and_transparent() -> None:
    """Installing twice keeps one router layer, and with nothing bound the
    router is indistinguishable from the stream underneath — same fd, writes
    pass straight through."""
    install_capture_routers()
    first_out, first_err = sys.stdout, sys.stderr
    install_capture_routers()

    assert sys.stdout is first_out, "second install wrapped the router again"
    assert sys.stderr is first_err, "second install wrapped the router again"
    assert isinstance(sys.stdout, _CaptureRouter)
    assert current_capture() is None
    # Delegation, not an io.TextIOBase stub: the stream underneath answers, so
    # a real fd comes back (an io.TextIOBase stub would raise here instead).
    assert isinstance(sys.stdout.fileno(), int)
    assert isinstance(sys.stderr.fileno(), int)


def test_bound_capture_takes_writes_and_unbinds() -> None:
    install_capture_routers()
    buf = io.StringIO()
    with capture_output(buf):
        print("inside")  # noqa: T201 — the behavior under test
        print("err", file=sys.stderr)  # noqa: T201
    print("outside")  # noqa: T201 — must NOT land in buf

    assert buf.getvalue() == "inside\nerr\n"
    assert current_capture() is None


def test_two_threads_capture_independently() -> None:
    """The isolation the hosted runner needs: two agents' execs run in two
    worker threads, each under its own copied context, and neither sees the
    other's output. A process-global assignment fails this by construction —
    whichever thread assigned last owns both."""
    install_capture_routers()
    bufs = {"a": io.StringIO(), "b": io.StringIO()}
    started = threading.Barrier(2)
    interleave = threading.Barrier(2)

    def body(name: str) -> None:
        with capture_output(bufs[name]):
            started.wait(timeout=5)
            print(f"{name}-1")  # noqa: T201
            interleave.wait(timeout=5)  # force the two captures to overlap
            print(f"{name}-2")  # noqa: T201

    threads = [
        threading.Thread(target=contextvars.copy_context().run, args=(body, name))
        for name in ("a", "b")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    assert bufs["a"].getvalue() == "a-1\na-2\n"
    assert bufs["b"].getvalue() == "b-1\nb-2\n"


async def test_capture_follows_to_thread_and_tasks() -> None:
    """Agent code that offloads to a thread or spawns a task keeps the same
    capture: both copy the current context. A `threading.local` would lose it
    at the first hop, which is why the binding is a contextvar."""
    install_capture_routers()
    buf = io.StringIO()

    def in_thread() -> None:
        print("from-thread")  # noqa: T201

    async def in_task() -> None:
        print("from-task")  # noqa: T201

    with capture_output(buf):
        await asyncio.to_thread(in_thread)
        await asyncio.create_task(in_task())

    assert buf.getvalue() == "from-thread\nfrom-task\n"


def test_abandoned_binding_never_reaches_the_process_streams() -> None:
    """The orphaned-worker case: a thread that never unwinds its `with` keeps
    its capture forever. Its context is its own, so the process's streams stay
    real — the property the faulthandler stall dump depends on."""
    install_capture_routers()
    buf = io.StringIO()
    entered = threading.Event()
    release = threading.Event()

    def never_unwinds() -> None:
        with capture_output(buf):
            entered.set()
            release.wait(timeout=10)

    t = threading.Thread(target=contextvars.copy_context().run, args=(never_unwinds,), daemon=True)
    t.start()
    assert entered.wait(timeout=5)
    try:
        assert current_capture() is None, "the abandoned binding leaked into the caller"
        sys.stdout.fileno()  # a capture buffer would raise here
        sys.stderr.fileno()
    finally:
        release.set()
        t.join(timeout=5)
