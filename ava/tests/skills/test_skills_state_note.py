"""Skill reads are noted in the exec turn's state update.

Skill recall must not offer a skill whose body the agent already has in its context window. The
body is read inside the exec child, so the identifier of every skill whose body is handed over
is appended to `ava.state_update["skill_reads"]` at the one point all read paths share (the
`skill_invoked` audit). The exec node commits it to the graph state; the skill-recall hook reads
it from there. See `agent/hooks/tests/test_skill_recall_hook.py` for the consumer.
"""

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import ava
from ava.sdk_surface.install import Installation
from ava.tests.skills._skills_helpers import _overlay_all_enabled as _overlay_all_enabled
from ava.tests.skills._skills_helpers import _write_skill
from ava.tests.skills._skills_helpers import fake_skills_dir as fake_skills_dir
from base import telemetry
from base.agents.context import AvaContext
from base.db import Database

pytestmark = pytest.mark.usefixtures("unit_home")

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _one_agent(_context: AvaContext | None = None) -> int:
    return 1


def _discard_audit(_db: Database, _event: telemetry.Event, **_kw: Any) -> None:
    return None


@pytest.fixture(autouse=True)
def _exec_turn(
    monkeypatch: pytest.MonkeyPatch, model_installation: Installation
) -> Iterator[dict[str, Any]]:
    """An exec turn is bound (the child's state slot and an empty state update), the agent has
    an identity, and the audit write goes nowhere."""
    monkeypatch.setattr(ava, "__plugin_installation__", model_installation, raising=False)
    monkeypatch.setattr("ava.sdk_surface.agent_identity.require_agent_id", _one_agent)
    monkeypatch.setattr("base.telemetry.audit_events.record_audit_reported", _discard_audit)
    ava.unbind_exec_turn()
    ava.state = object()
    update: dict[str, Any] = {}
    ava.state_update = update
    yield update
    ava.unbind_exec_turn()


def _skills(root: Path) -> None:
    _write_skill(root, "alpha", "name: alpha\ndescription: a", body="# A\n")
    group = root / "grp"
    group.mkdir()
    _write_skill(group, "beta", "name: beta\ndescription: b", body="# B\n")


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(lambda: ava.skills.alpha.__doc__, id="doc"),
        pytest.param(lambda: ava.help(ava.skills.alpha), id="help"),
        pytest.param(lambda: ava.files.read(ava.skills.alpha.path + "/SKILL.md"), id="files-read"),
    ],
)
def test_every_way_of_reading_a_body_notes_the_skill(
    fake_skills_dir: Path, _exec_turn: dict[str, Any], read: Any
) -> None:
    _skills(fake_skills_dir)

    read()

    assert _exec_turn == {"skill_reads": ["alpha"]}


def test_a_namespaced_skill_is_noted_by_its_display_identifier(
    fake_skills_dir: Path, _exec_turn: dict[str, Any]
) -> None:
    _skills(fake_skills_dir)

    _ = ava.skills.grp.beta.__doc__

    assert _exec_turn == {"skill_reads": ["grp:beta"]}


def test_reading_a_body_twice_notes_it_once_and_two_skills_note_both(
    fake_skills_dir: Path, _exec_turn: dict[str, Any]
) -> None:
    _skills(fake_skills_dir)

    _ = ava.skills.alpha.__doc__
    _ = ava.skills.grp.beta.__doc__
    _ = ava.skills.alpha.__doc__

    assert _exec_turn == {"skill_reads": ["alpha", "grp:beta"]}


def test_resolving_or_listing_a_skill_notes_nothing(
    fake_skills_dir: Path, _exec_turn: dict[str, Any]
) -> None:
    """Exposure is not use: only a body that was handed over counts as read."""
    _skills(fake_skills_dir)

    leaf = ava.skills.alpha
    _ = leaf._description, leaf.path
    ava.help(ava.skills)

    assert _exec_turn == {}


def test_outside_an_exec_turn_a_read_succeeds_and_notes_nothing(fake_skills_dir: Path) -> None:
    _skills(fake_skills_dir)
    ava.unbind_exec_turn()

    assert "# A" in (ava.skills.alpha.__doc__ or "")


def test_agent_code_that_replaced_the_update_does_not_break_the_read(
    fake_skills_dir: Path,
) -> None:
    """The exec child reports a replaced `ava.state_update` at exit; the read itself is not
    the place to fail."""
    _skills(fake_skills_dir)
    ava.state_update = "not a dict"  # pyright: ignore[reportAttributeAccessIssue]

    assert "# A" in (ava.skills.alpha.__doc__ or "")


def test_the_audit_is_still_written_when_the_read_is_noted(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skills(fake_skills_dir)
    recorded: list[str] = []

    def capture(_db: Database, event: telemetry.Event, **_kw: Any) -> None:
        recorded.append(event.attributes["skill"])

    monkeypatch.setattr("base.telemetry.audit_events.record_audit_reported", capture)

    _ = ava.skills.alpha.__doc__

    assert recorded == ["alpha"]


# A read in the exec child must cost the child no network or decision stack: it runs on every
# execute_code that opens a skill, and the child's start is kept off those imports on purpose.
# `import ava` already holds httpx and the decision value types, so the probe reports what the
# read itself adds, with the audit write and the settings root stubbed (both far heavier than
# the note, and covered elsewhere).
_PROBE = """
import json
import sys
import types

root, home = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
import ava
import ava.sdk_surface.agent_identity as identity

identity.require_agent_id = lambda _context=None: 1
import base.packages.extensions.install_registry as registry

registry.loadable_skill_names = lambda: {"alpha", "grp"}
settings = types.ModuleType("ava.sdk_surface.settings")
settings.database = lambda: None
sys.modules["ava.sdk_surface.settings"] = settings
audit = types.ModuleType("base.telemetry.audit_events")
audit.SkillInvokedPayload = lambda **kw: types.SimpleNamespace(model_dump=lambda: kw)
audit.prepare_event_log = lambda **kw: kw
audit.record_audit_reported = lambda *a, **kw: None
sys.modules["base.telemetry.audit_events"] = audit

ava.state = object()
ava.state_update = {}
import ava.skills

before = set(sys.modules)
ava.skills.alpha.__doc__
forbidden = ("httpx", "langchain", "langgraph", "agent", "base.lm.catalog.decide.call")
print(json.dumps({
    "update": ava.state_update,
    "new": sorted(m for m in set(sys.modules) - before if m.startswith(forbidden)),
}))
"""


def test_noting_a_read_in_the_child_imports_no_network_or_decision_stack(
    fake_skills_dir: Path, unit_home: Path
) -> None:
    _skills(fake_skills_dir)
    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [sys.executable, "-I", "-B", "-X", "utf8", "-c", _PROBE, str(_REPO_ROOT), str(unit_home)],
        cwd=_REPO_ROOT,
        env={"HOME": str(unit_home), "AVA_HOME": str(unit_home)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["update"] == {"skill_reads": ["alpha"]}
    assert report["new"] == []
