"""The `files.write` / `files.append` wraps record the memory notes an exec call wrote.

The wraps are transparent: they hand back what the layer below returned, let its exception through
untouched, and never let their own failure reach the caller. They record only after a write that
succeeded, and only `.md` files under the shared pool or the agent's own memory folder.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest

import ava
from agent.state import build_agent_state
from ava import gateway_client
from ava.sdk_surface import install
from base.packages.plugins.extensions import ExtensionRegistry
from tests.fixtures.pin_agent import pin_agent

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SHARED = "ava_memory__shared_notes"
_PERSONAL = "ava_memory__personal_notes"


def _registry() -> ExtensionRegistry:
    from ava_builtins.plugins.ava_code import agent_runtime as code_runtime
    from ava_builtins.plugins.ava_code import plugin as code_plugin
    from ava_builtins.plugins.ava_memory import agent_runtime as memory_runtime
    from ava_builtins.plugins.ava_memory import plugin as memory_plugin

    # Name order is the loader's order: ava_memory's wraps sit outside ava_code's.
    return ExtensionRegistry(
        (
            ("ava_code", code_plugin.contribute().merged(code_runtime.contribute())),
            ("ava_memory", memory_plugin.contribute().merged(memory_runtime.contribute())),
        )
    )


@pytest.fixture
def exec_turn(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The wraps installed over a bound exec turn of agent 17; yields the home."""
    from base.agents.sdk import call_policy

    def _policy_for_test(_owner: call_policy.SamplingPolicyOwner) -> call_policy.SamplingPolicy:
        return call_policy.SamplingPolicy()

    monkeypatch.setattr(call_policy, "policy", _policy_for_test)

    def no_request(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the exec subprocess must send no gateway request")

    monkeypatch.setattr(gateway_client, "post", no_request)
    monkeypatch.setattr(gateway_client, "memory_search", no_request)
    pin_agent(17)
    registry = _registry()
    install.install(registry)
    ava.state = build_agent_state(registry)(messages=[], halted=False)
    ava.state_update = {}
    yield unit_home
    ava.unbind_exec_turn()
    install.uninstall()


def _recorded(key: str) -> set[str]:
    update: dict[str, set[str]] = ava.state_update  # pyright: ignore[reportAssignmentType]
    return set(update.get(key, set()))


def test_wraps_stack_over_the_cwd_wraps_of_the_same_targets(exec_turn: Path) -> None:
    for target in ("files.write", "files.append", "files.edit"):
        assert [plugin for plugin, _ in ava.extend.stack(target)] == ["ava_code", "ava_memory"]


def test_a_shared_note_is_recorded_relative_to_the_pool(exec_turn: Path) -> None:
    ava.files.write(ava.memory.PATH / "topics" / "rule.md", "---\n---\n")
    assert _recorded(_SHARED) == {"topics/rule.md"}
    assert _recorded(_PERSONAL) == set()
    assert (ava.memory.PATH / "topics" / "rule.md").read_text() == "---\n---\n"


def test_a_personal_note_is_recorded_apart_from_shared_ones(exec_turn: Path) -> None:
    ava.files.write(ava.memory.PERSONAL_PATH / "mine.md", "text")
    assert _recorded(_PERSONAL) == {"mine.md"}
    assert _recorded(_SHARED) == set()


def test_append_records_the_note_too(exec_turn: Path) -> None:
    ava.files.append(ava.memory.PATH / "log-ish.md", "line\n")
    ava.files.append(ava.memory.PATH / "log-ish.md", "again\n")
    assert _recorded(_SHARED) == {"log-ish.md"}
    assert (ava.memory.PATH / "log-ish.md").read_text() == "line\nagain\n"


def test_an_in_place_edit_records_the_note(exec_turn: Path) -> None:
    note = ava.memory.PATH / "topics" / "rule.md"
    note.parent.mkdir(parents=True)
    note.write_text("alpha beta\n")
    ava.files.edit(note, "beta", "gamma")
    assert _recorded(_SHARED) == {"topics/rule.md"}
    assert note.read_text() == "alpha gamma\n"
    ava.files.edit(ava.memory.PATH / "topics" / "rule.md", "alpha", "delta", replace_all=True)
    assert note.read_text() == "delta gamma\n"


def test_a_failed_edit_raises_unchanged_and_records_nothing(exec_turn: Path) -> None:
    note = ava.memory.PERSONAL_PATH / "mine.md"
    note.parent.mkdir(parents=True)
    note.write_text("text\n")
    with pytest.raises(ValueError, match="old not found"):
        ava.files.edit(note, "absent", "x")
    assert _recorded(_PERSONAL) == set()


def test_several_writes_of_one_call_merge(exec_turn: Path) -> None:
    ava.files.write(ava.memory.PATH / "a.md", "a")
    ava.files.write(ava.memory.PATH / "b.md", "b")
    ava.files.write(ava.memory.PERSONAL_PATH / "c.md", "c")
    assert _recorded(_SHARED) == {"a.md", "b.md"}
    assert _recorded(_PERSONAL) == {"c.md"}


def test_a_relative_path_follows_the_tracked_cwd(exec_turn: Path) -> None:
    ava.memory.PATH.mkdir(parents=True)
    ava.cwd.set(ava.memory.PATH)
    ava.files.write("by-cwd.md", "x")
    assert _recorded(_SHARED) == {"by-cwd.md"}
    assert (ava.memory.PATH / "by-cwd.md").exists()


@pytest.mark.parametrize(
    "where",
    [
        "pool/notes.txt",
        "pool/.hidden/rule.md",
        "pool/topics/.draft/rule.md",
        "home/elsewhere/rule.md",
    ],
)
def test_other_files_are_not_recorded_and_still_written(exec_turn: Path, where: str) -> None:
    root, _, rest = where.partition("/")
    target = (ava.memory.PATH if root == "pool" else exec_turn) / rest
    ava.files.write(target, "content")
    assert _recorded(_SHARED) == set() and _recorded(_PERSONAL) == set()
    assert target.read_text() == "content"


def test_a_failed_write_raises_unchanged_and_records_nothing(exec_turn: Path) -> None:
    with pytest.raises(TypeError, match="content"):
        ava.files.write(ava.memory.PATH / "bad.md", 123)  # pyright: ignore[reportArgumentType]
    assert _recorded(_SHARED) == set()


def test_the_wrap_returns_what_the_layer_below_returns() -> None:
    from ava_builtins.plugins.ava_memory import plugin

    sentinel = object()
    calls: list[tuple[str, str]] = []

    def inner(path: str, content: str) -> object:
        calls.append((path, content))
        return sentinel

    wrapped_write = cast("Callable[..., Any]", plugin._wrapped_write)
    wrapped_append = cast("Callable[..., Any]", plugin._wrapped_append)
    wrapped_edit = cast("Callable[..., Any]", plugin._wrapped_edit)
    assert wrapped_write(inner, "x.md", "c") is sentinel
    assert wrapped_append(inner, "x.md", "c") is sentinel
    edit_calls: list[tuple[Any, ...]] = []

    def edit_inner(*args: Any, **kwargs: Any) -> object:
        edit_calls.append((args, kwargs))
        return sentinel

    assert wrapped_edit(edit_inner, "x.md", "a", "b", replace_all=True) is sentinel
    assert edit_calls == [(("x.md", "a", "b"), {"replace_all": True})]
    assert calls == [("x.md", "c"), ("x.md", "c")]


def test_the_exception_of_the_layer_below_propagates_as_is() -> None:
    from ava_builtins.plugins.ava_memory import plugin

    failure = PermissionError("read-only")

    def inner(_path: str, _content: str) -> None:
        raise failure

    with pytest.raises(PermissionError) as raised:
        plugin._wrapped_write(inner, "x.md", "c")
    assert raised.value is failure


def test_a_failure_while_recording_is_logged_and_does_not_touch_the_write(
    exec_turn: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ava_builtins.plugins.ava_memory import written_notes

    logged: list[str] = []

    class _Logger:
        def opt(self, **_kwargs: Any) -> _Logger:
            return self

        def warning(self, message: str, *args: Any) -> None:
            logged.append(message.format(*args))

    monkeypatch.setattr(written_notes, "logger", _Logger())
    ava.state_update = "tampered"  # the state update the record is merged into is unusable

    note = ava.memory.PATH / "survives.md"
    assert ava.files.write(note, "kept") is None
    assert note.read_text() == "kept"
    assert len(logged) == 1 and "survives.md" in logged[0]


def test_outside_an_exec_turn_nothing_is_recorded_and_nothing_fails(exec_turn: Path) -> None:
    ava.unbind_exec_turn()
    note = ava.memory.PATH / "x.md"
    assert ava.files.write(note, "t") is None
    assert note.read_text() == "t"
    assert not ava.in_exec_turn()


_BOUNDARY_PROBE = """
import json
import sys

sys.path.insert(0, sys.argv[1])
import ava  # noqa: F401

loaded_with_ava = set(sys.modules)
import ava_builtins.plugins.ava_memory.plugin  # the exec subprocess's face of the plugin
import ava_builtins.plugins.ava_memory.written_notes

# Absolute: the decision call stack, services, the agent runtime and langchain/langgraph never load.
absolute = ("base.lm.decision.call", "base.lm.catalog.decide.http", "services.", "agent.",
            "langchain", "langgraph")
# Relative: `import ava` itself already loads httpx (the gateway client's transport), so the
# plugin's face may add nothing on top of that.
added = set(sys.modules) - loaded_with_ava
print(json.dumps({
    "absolute": sorted(n for n in sys.modules if n.startswith(absolute)),
    "added_httpx": sorted(n for n in added if n.startswith("httpx")),
}))
"""


def test_the_subprocess_face_imports_no_decision_stack_agent_runtime_or_new_http_client() -> None:
    """The wraps run in the exec subprocess: the plugin's face brings in no decision-call code, no
    service, no agent-runtime module, and no httpx beyond what `import ava` loads."""
    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [sys.executable, "-I", "-B", "-X", "utf8", "-c", _BOUNDARY_PROBE, str(_REPO_ROOT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == {"absolute": [], "added_httpx": []}
