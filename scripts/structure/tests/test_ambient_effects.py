"""Unit coverage for the ambient-state rule's effects and boundaries (scripts/structure/
ambient_state/): what a module reads or calls while it imports, free-floating background work, the
rule's scope, and the closed lists with their stale entries. State shapes are covered in
test_ambient_state.py; the gate wiring in scripts/lint/tests/test_ambient_state_gate.py."""

from __future__ import annotations

import ast
import pathlib
import textwrap

import pytest

from scripts.structure import ambient_state
from scripts.structure.ambient_state import allowlist as allow


def _write(root: pathlib.Path, name: str, content: str) -> pathlib.Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def _sites(
    root: pathlib.Path,
    source: str,
    rel: str = "base/mod.py",
    files: dict[str, str] | None = None,
) -> dict[str, int]:
    """`rule:name -> site count` for one module; `files` are other repo files it can see."""
    for name, content in (files or {}).items():
        _write(root, name, content)
    path = _write(root, rel, source)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    measured = ambient_state.measure(tree, rel, root)
    return {key.split("::", 1)[1]: len(lines) for key, lines in measured.items()}


# --- what a module reads while it imports -----------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "TIMEOUT = settings.services.timeout",
        "TIMEOUT = float(settings.services.timeout)",
        "HOME = os.environ['HOME']",
        "HOME = os.environ.get('HOME', '')",
        "HOME = os.getenv('HOME')",
        "STARTED = time.time()",
        "ROOT = Path(os.environ['ROOT'])",
        "ME = Path.home()",
        "TEXT = Path('x').read_text()",
    ],
)
def test_reading_the_environment_while_importing_is_reported(
    tmp_path: pathlib.Path, line: str
) -> None:
    header = "import os, time\nfrom pathlib import Path\nfrom base.config import settings\n\n"
    sites = _sites(tmp_path, header + line + "\n")

    assert len(sites) == 1
    assert next(iter(sites)).startswith("import-time-read:")


def test_a_settings_name_that_is_not_the_config_object_is_not_a_read(
    tmp_path: pathlib.Path,
) -> None:
    source = "from mylib import settings\n\nTIMEOUT = settings.timeout\n"

    assert _sites(tmp_path, source) == {}


def test_a_lazy_read_inside_a_function_or_lambda_is_not_import_time(
    tmp_path: pathlib.Path,
) -> None:
    source = """
        import os
        from functools import partial

        def home():
            return os.environ["HOME"]

        FETCH = lambda: os.environ["HOME"]
    """
    assert _sites(tmp_path, source) == {}


@pytest.mark.parametrize(
    "line",
    [
        "IS_WINDOWS = sys.platform == 'win32'",
        "IS_LINUX = sys.platform.startswith('linux')",
        "ON_NT = os.name == 'nt'",
        "SYSTEM = platform.system()",
    ],
)
def test_a_platform_constant_is_a_host_fact(tmp_path: pathlib.Path, line: str) -> None:
    sites = _sites(tmp_path, f"import os, platform, sys\n\n{line}\n")

    assert len(sites) == 1
    assert next(iter(sites)).startswith("host-fact:")


# --- calls that run at import ------------------------------------------------------------------------


def test_a_bare_call_at_import_is_reported_under_its_resolved_name(
    tmp_path: pathlib.Path,
) -> None:
    source = """
        from base.telemetry import register_metric

        register_metric("a")
        register_metric("b")
    """
    assert _sites(tmp_path, source) == {"import-time-call:base.telemetry.register_metric": 2}


def test_a_conditional_call_at_import_is_still_import_time(tmp_path: pathlib.Path) -> None:
    source = """
        import atexit

        try:
            atexit.register(print)
        except OSError:
            pass
    """
    assert _sites(tmp_path, source) == {"import-time-call:atexit.register": 1}


def test_declarative_wiring_and_main_blocks_are_not_import_time_calls(
    tmp_path: pathlib.Path,
) -> None:
    source = """
        import argparse
        from fastapi import FastAPI
        from base.routes import router

        app = FastAPI()
        app.include_router(router)
        parser = argparse.ArgumentParser()
        parser.add_argument("--x")

        if __name__ == "__main__":
            main()
    """
    assert _sites(tmp_path, source) == {}


# --- free-floating background work ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        "asyncio.create_task(work())",
        "asyncio.ensure_future(work())",
        "create_task(work())",
        "loop.create_task(work())",
        "asyncio.get_running_loop().create_task(work())",
        "asyncio.get_event_loop().create_task(work())",
        "self._loop.create_task(work())",
        "event_loop.create_task(work())",
    ],
)
def test_a_task_started_on_asyncio_or_a_loop_is_reported_by_function(
    tmp_path: pathlib.Path, call: str
) -> None:
    source = f"""
        import asyncio
        from asyncio import create_task

        async def work(): ...

        class Service:
            async def start(self, loop, event_loop):
                {call}
    """
    assert _sites(tmp_path, source) == {"asyncio-task:Service.start": 1}


def test_a_loop_alias_is_followed(tmp_path: pathlib.Path) -> None:
    source = """
        import asyncio

        def kick(work):
            runner = asyncio.get_running_loop()
            runner.create_task(work())
    """
    assert _sites(tmp_path, source) == {"asyncio-task:kick": 1}


def test_tasks_are_counted_per_site_under_the_enclosing_function(tmp_path: pathlib.Path) -> None:
    source = """
        import asyncio

        async def fan_out(jobs):
            asyncio.create_task(jobs[0]())
            asyncio.create_task(jobs[1]())

            def inner():
                asyncio.ensure_future(jobs[2]())
    """
    assert _sites(tmp_path, source) == {
        "asyncio-task:fan_out": 2,
        "asyncio-task:fan_out.inner": 1,
    }


@pytest.mark.parametrize(
    "call",
    ["tg.create_task(work())", "self._group.create_task(work())", "registry.create_task(spec)"],
)
def test_a_create_task_on_a_taskgroup_or_another_receiver_is_not_reported(
    tmp_path: pathlib.Path, call: str
) -> None:
    """AST cannot prove the receiver is a TaskGroup: any receiver that is neither `asyncio`
    nor named like a loop passes. That is a known gap, written down in the rule's docs."""
    source = f"""
        import asyncio

        async def run(self, registry, spec, work):
            async with asyncio.TaskGroup() as tg:
                {call}
    """
    assert _sites(tmp_path, source) == {}


@pytest.mark.parametrize(
    "call", ["threading.Thread(target=run).start()", "Thread(target=run, daemon=True)"]
)
def test_a_thread_is_reported_by_function(tmp_path: pathlib.Path, call: str) -> None:
    source = f"""
        import threading
        from threading import Thread

        def run(): ...

        def launch():
            {call}
    """
    assert _sites(tmp_path, source) == {"thread:launch": 1}


# --- scope --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "governed"),
    [
        ("base/mod.py", True),
        ("schedules/daily.py", True),
        ("ava_builtins/plugins/ava_fleet/runtime.py", True),
        ("base/tests/test_mod.py", False),
        ("tests/components/base/test_mod.py", False),
        ("base/__main__.py", False),
        ("ava_builtins/skills/web/scripts/fetch.py", False),
        ("ava_builtins/plugins/ava_code/skills/review/scripts/run.py", False),
        ("scripts/lint/x.py", False),
        ("ui/web/x.py", False),
    ],
)
def test_the_rule_governs_the_packages_and_schedules_not_tests_or_skill_scripts(
    rel: str, governed: bool
) -> None:
    assert ambient_state.in_scope(rel) is governed


def test_an_out_of_scope_module_measures_nothing(tmp_path: pathlib.Path) -> None:
    source = "_REGISTRY = {}\n_CLIENT = make()\n"

    assert _sites(tmp_path, source, rel="base/tests/test_mod.py") == {}
    assert _sites(tmp_path, source, rel="ava_builtins/skills/web/scripts/run.py") == {}
    assert _sites(tmp_path, source, rel="schedules/daily.py") == {
        "ambient-container:_REGISTRY": 1,
        "ambient-instance:_CLIENT": 1,
    }


# --- the closed lists -----------------------------------------------------------------------------------


def test_a_sink_facade_file_lets_its_state_through_but_not_its_threads(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = """
        import threading

        _BUFFER = []

        def start():
            threading.Thread(target=start).start()
    """
    assert _sites(tmp_path, source, rel="base/log/sink.py") == {
        "ambient-container:_BUFFER": 1,
        "thread:start": 1,
    }

    monkeypatch.setattr(allow, "SINK_FACADES", {"base/log/sink.py": "the log sink's buffer"})
    assert _sites(tmp_path, source, rel="base/log/sink.py") == {"thread:start": 1}


def _stale(root: pathlib.Path, rel: str, source: str) -> list[str]:
    path = _write(root, rel, source)
    errors = ambient_state.allowlist_errors(ast.parse(path.read_text()), rel, root)
    return [message for _, message in errors]


def test_a_list_entry_whose_site_is_gone_is_stale(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(allow, "ALLOWED", {"base/mod.py::hidden-singleton:table": "static"})
    monkeypatch.setattr(allow, "SINK_FACADES", {"base/mod.py": "buffer"})
    live = "from functools import cache\n\n_warned = set()\n\n@cache\ndef table(): ...\n"

    assert _stale(tmp_path, "base/mod.py", live) == []

    problems = _stale(tmp_path, "base/mod.py", "VALUE = 1\n")
    assert any(
        "hidden-singleton:table" in message and "site is gone" in message for message in problems
    )
    assert any(
        "SINK_FACADES entry" in message and "no module-level state" in message
        for message in problems
    )


def test_a_list_entry_for_a_missing_file_or_function_is_stale(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(allow, "ALLOWED", {"base/gone.py::hidden-singleton:table": "static"})
    monkeypatch.setattr(allow, "SINK_FACADES", {"base/also_gone.py": "buffer"})
    monkeypatch.setattr(
        allow, "PURE_REPO_CALLEES", {"base.keys.derive": "constant", "base.keys.vanished": "x"}
    )
    _write(tmp_path, "base/keys.py", "def derive():\n    return 1\n")

    errors = ambient_state.missing_allowlist_errors(tmp_path)

    assert any(error.startswith("base/gone.py:1:") for error in errors)
    assert any(error.startswith("base/also_gone.py:1:") for error in errors)
    assert any("PURE_REPO_CALLEES entry base.keys.vanished" in error for error in errors)
    assert not any("base.keys.derive" in error for error in errors)


def test_a_pure_repo_callee_is_exempt_where_it_is_assigned(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = "from base.keys import derive\n\nDEFAULT = derive('x')\n"
    assert _sites(tmp_path, source) == {"ambient-instance:DEFAULT": 1}

    monkeypatch.setattr(allow, "PURE_REPO_CALLEES", {"base.keys.derive": "a fixed value"})
    assert _sites(tmp_path, source) == {}


def test_the_closed_lists_carry_a_reason_for_every_entry() -> None:
    for name in ("SINK_FACADES", "ALLOWED", "PURE_REPO_CALLEES"):
        entries: dict[str, str] = getattr(allow, name)
        assert all(reason.strip() for reason in entries.values()), name


@pytest.mark.parametrize(
    ("target", "known"),
    [
        ("ambient-container:_REGISTRY", True),
        ("asyncio-task:Service.start", True),
        ("contextvar:_V", True),
        ("made-up-rule:x", False),
        ("ambient-container:", False),
        ("ambient-container", False),
    ],
)
def test_a_baseline_key_must_name_a_known_rule_and_a_name(target: str, known: bool) -> None:
    assert ambient_state.is_target(target) is known
