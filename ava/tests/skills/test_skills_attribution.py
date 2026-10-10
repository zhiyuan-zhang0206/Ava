"""ava.skills attribution: the curated index render, SKILL.md read consumption (ava.help / ava.skills.read / files.read), and invocation recording gated on the write landing; split from ava/tests/skills/test_skills.py (task #4922)."""

from pathlib import Path
from typing import Any

import psycopg
import pytest

import ava.skills as skills_mod
from ava.sdk_surface.install import Installation
from ava.tests.skills._skills_helpers import _overlay_all_enabled as _overlay_all_enabled
from ava.tests.skills._skills_helpers import _write_skill
from ava.tests.skills._skills_helpers import fake_skills_dir as fake_skills_dir
from base import telemetry
from base.agents.context import AvaContext
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.log import logger


def _one_agent(_context: AvaContext | None = None) -> int:
    return 1


# Every test runs in a per-test unit home whose `skills/` does not exist by
# default, so the real ~/.agents/skills/ never leaks into a scan; the
# fake_skills_dir fixture creates the load dir.
pytestmark = pytest.mark.usefixtures("unit_home")


# ─── index render: curated surface + attribution ─────────────────────────


def test_all_for_ava_is_the_live_top_level_index(fake_skills_dir: Path) -> None:
    """`ava.skills.__all_for_ava__` is the curated agent-visible surface — the
    live top-level skill/namespace names, not whatever `dir()` happens to hold.
    `agent_visible_names` (the one accessor help / SDK-expand / metering share)
    must see it, which is why it is a property on the module's own class:
    the accessor reads it with `getattr_static`, which never runs a PEP 562
    module `__getattr__`."""
    import ava

    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a")
    d = fake_skills_dir / "grp"
    d.mkdir()
    _write_skill(d, "beta", "name: beta\ndescription: b")

    # The surface is skill names plus the `read` utility — the one non-skill
    # member, so `ava.help(ava.skills)` renders its contract next to the index.
    assert skills_mod.__all_for_ava__ == ["alpha", "grp", "read"]
    assert ava.agent_visible_names(skills_mod) == ["alpha", "grp", "read"]


def test_help_on_skills_module_is_index_only(
    fake_skills_dir: Path,
    capsys: pytest.CaptureFixture[str],
    model_installation: Installation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ava.help(ava.skills)` renders an INDEX: per entry its `ava.skills.<path>`
    heading plus its one-line description — never a SKILL.md body. The body is
    one `ava.help(ava.skills.<name>)` away; rendering it here would put the whole
    catalog into the prompt."""
    import ava

    monkeypatch.setattr(ava, "__plugin_installation__", model_installation, raising=False)

    _write_skill(
        fake_skills_dir,
        "alpha",
        "name: alpha\ndescription: Alpha desc",
        body="# Alpha body\n\nSECRET_BODY_MARKER\n",
    )
    ava.help(ava.skills)
    out = capsys.readouterr().out
    assert "## ava.skills.alpha\n\nAlpha desc" in out
    assert "SECRET_BODY_MARKER" not in out


def test_resolution_is_not_consumption(
    fake_skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_installation: Installation,
) -> None:
    """Mere access to a skill must not emit `skill_invoked`: `ava.skills.<name>`
    resolution, description reads, dir() and printing a proxy all record
    nothing — the old trigger fired on node resolution, so an agent that only
    glanced at the catalog (or named a skill in planning without ever opening
    it) claimed a fake "loaded" row (measured: ~70% of rows had no usage trace
    in the transcript). The signal fires on first SKILL.md body consumption —
    help() or a direct `__doc__` read."""
    import ava

    monkeypatch.setattr(ava, "__plugin_installation__", model_installation, raising=False)

    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    d = fake_skills_dir / "grp"
    d.mkdir()
    _write_skill(d, "beta", "name: beta\ndescription: b", body="# B\n")
    root = fake_skills_dir / "guide"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: guide\ndescription: g\n---\n\n# Guide body\n", encoding="utf-8"
    )
    _write_skill(root, "sub", "name: sub\ndescription: s", body="# Sub\n")

    recorded: list[tuple[str, str]] = []

    def capture(_db: Database, event: telemetry.Event) -> None:
        recorded.append((event.attributes["skill"], event.attributes["invocation_depth"]))

    monkeypatch.setattr("ava.sdk_surface.agent_identity.require_agent_id", _one_agent)
    monkeypatch.setattr("base.telemetry.audit_events.record_audit_reported", capture)

    # Resolution and metadata reads — no body enters the conversation.
    leaf = ava.skills.alpha
    _ = leaf._description, leaf.path, leaf.name
    dir(leaf)
    repr(leaf)
    ns = ava.skills.guide
    _ = ns._description, ns.path
    dir(ns)
    assert recorded == []

    # First body consumption is the signal — and only once (the proxy caches its body).
    _ = leaf.__doc__
    assert [n for n, _d in recorded] == ["alpha"]
    _ = leaf.__doc__  # cached
    assert [n for n, _d in recorded] == ["alpha"]

    # A root-skill namespace attributes only when ITS body is consumed (help),
    # never for resolving it or listing its children.
    recorded.clear()
    ava.help(ava.skills.guide)
    assert [n for n, _d in recorded] == ["guide"]


def test_index_render_records_no_loaded_attribution(
    fake_skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_installation: Installation,
) -> None:
    """Listing the catalog is not using a skill. `ava.help(ava.skills)` resolves
    every node but opens no body, so it must emit no `skill_invoked` row —
    "loaded" is the only depth ava_self_evolution scores, and a fake row per
    installed skill would bury the real signal. Node resolution records
    nothing at all (the signal fires on first SKILL.md body consumption in the
    lazy `__doc__` loaders), and the index walk reads only frontmatter
    `_description`s — so an index render is silent by construction.

    The catalog deliberately contains a ROOT SKILL (a folder carrying its own
    SKILL.md *and* children) as well as a leaf and a plain namespace: root
    skills are the shape of the skills agents reach for most (ava_guide,
    ava_fleet, ava_memory all have it), and they are the risky case — their
    `_description` renders in indexes without loading the body, while
    `ava.help(ava.skills.guide)` consumes the body and MUST attribute."""
    import ava

    monkeypatch.setattr(ava, "__plugin_installation__", model_installation, raising=False)

    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    d = fake_skills_dir / "grp"
    d.mkdir()
    _write_skill(d, "beta", "name: beta\ndescription: b", body="# B\n")
    # Root skill: SKILL.md at the folder level plus a child skill underneath.
    root = fake_skills_dir / "guide"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: guide\ndescription: g\n---\n\n# Guide body\n", encoding="utf-8"
    )
    _write_skill(root, "sub", "name: sub\ndescription: s", body="# Sub\n")

    recorded: list[tuple[str, str]] = []

    def capture(_db: Database, event: telemetry.Event) -> None:
        recorded.append((event.attributes["skill"], event.attributes["invocation_depth"]))

    monkeypatch.setattr("ava.sdk_surface.agent_identity.require_agent_id", _one_agent)
    monkeypatch.setattr("base.telemetry.audit_events.record_audit_reported", capture)

    ava.help(ava.skills)  # walks the leaf, the plain namespace AND the root skill
    assert recorded == []
    ava.help(ava.skills.grp)  # a namespace listing is an index too
    assert recorded == []
    ava.help(ava.skills.guide)  # a root skill's own body + its child index
    assert [n for n, _d in recorded] == ["guide"]  # itself only — never its child

    # A deliberate access to one skill is the genuine "loaded" signal.
    recorded.clear()
    ava.help(ava.skills.alpha)
    assert recorded == [("alpha", "loaded")]


# ─── direct SKILL.md file reads attribute (the .path + files.read pattern) ──


def test_files_read_skill_md_records_consumption(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `.path` + `ava.files.read(SKILL.md)` pattern consumes a skill body
    but bypasses the lazy proxy `__doc__` hook — it must still record one
    `skill_invoked` row, or the `loaded` signal is systematically under-counted
    (measured: 68 agents / 3 days used the pattern, 13 with zero rows)."""
    import ava

    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    skill_dir = fake_skills_dir / "alpha"
    (skill_dir / "notes.md").write_text("# notes\n", encoding="utf-8")

    recorded: list[tuple[str, str]] = []

    def capture(_db: Database, event: telemetry.Event) -> None:
        recorded.append((event.attributes["skill"], event.attributes["invocation_depth"]))

    monkeypatch.setattr("ava.sdk_surface.agent_identity.require_agent_id", _one_agent)
    monkeypatch.setattr("base.telemetry.audit_events.record_audit_reported", capture)

    # The exact agent pattern: proxy.path + read of SKILL.md.
    out = ava.files.read(ava.skills.alpha.path + "/SKILL.md")
    assert out.endswith("# A\n") and "name: alpha" in out
    assert recorded == [("alpha", "loaded")]

    recorded.clear()
    # Range reads consume too — a partial body is still the body.
    ava.files.read(ava.skills.alpha.path + "/SKILL.md", start=1, end=1)
    assert recorded == [("alpha", "loaded")]


def test_files_read_other_files_do_not_record(
    fake_skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_installation: Installation,
) -> None:
    """Only a loaded skill's own SKILL.md attributes: sibling files in the
    skill directory, a SKILL.md outside the mounted tree, and index renders
    must stay silent (a random SKILL.md is not a skill the agent loaded)."""
    import ava

    monkeypatch.setattr(ava, "__plugin_installation__", model_installation, raising=False)

    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    skill_dir = fake_skills_dir / "alpha"
    (skill_dir / "notes.md").write_text("# notes\n", encoding="utf-8")

    recorded: list[tuple[str, str]] = []

    def capture(_db: Database, event: telemetry.Event) -> None:
        recorded.append((event.attributes["skill"], event.attributes["invocation_depth"]))

    monkeypatch.setattr("ava.sdk_surface.agent_identity.require_agent_id", _one_agent)
    monkeypatch.setattr("base.telemetry.audit_events.record_audit_reported", capture)

    # Sibling file inside the skill dir — skill art, not the body.
    ava.files.read(str(skill_dir / "notes.md"))
    assert recorded == []

    # A SKILL.md on disk but outside the loaded tree (not mounted).
    stray = fake_skills_dir.parent / "stray"
    stray.mkdir()
    (stray / "SKILL.md").write_text("---\nname: stray\ndescription: s\n---\n", encoding="utf-8")
    ava.files.read(str(stray / "SKILL.md"))
    assert recorded == []

    # An index render never opens a body — nothing to record (a deliberate
    # `help(ava.skills.alpha)` WOULD record; that coverage lives below).
    ava.help(ava.skills)
    assert recorded == []


def test_every_consumption_records_one_event_and_nothing_is_remembered(
    fake_skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_installation: Installation,
) -> None:
    """Attribution is an event written at the moment of consumption, not a
    per-run dedup: the SDK keeps no record of what it already wrote, so each
    consumption — a direct SKILL.md read, the same skill through the proxy, a
    second read — emits its own `skill_invoked` event. The consumer reads the
    set of skills, so repeats are harmless; remembering them was the buffer."""
    import ava

    monkeypatch.setattr(ava, "__plugin_installation__", model_installation, raising=False)

    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    monkeypatch.setattr("ava.sdk_surface.agent_identity.require_agent_id", _one_agent)

    written: list[Any] = []

    def _capture(_db: object, event: Any) -> None:
        written.append(event)

    monkeypatch.setattr("base.telemetry.audit_events.record_audit_reported", _capture)

    path = ava.skills.alpha.path + "/SKILL.md"
    ava.files.read(path)
    ava.files.read(path)
    ava.help(ava.skills.alpha)  # the same skill through the proxy
    assert [e.attributes["skill"] for e in written] == ["alpha"] * 3
    assert not hasattr(skills_mod, "_recorded_skill_invocations")


def test_files_read_skill_md_silent_outside_agent(fake_skills_dir: Path) -> None:
    """Outside an agent process (no bound identity) `require_agent_id` raises
    and attribution skips silently — the read itself keeps working (skill
    attribution is telemetry, and this helper must never break a file read)."""
    import ava

    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    out = ava.files.read(ava.skills.alpha.path + "/SKILL.md")
    assert out.endswith("# A\n")  # no raise; body still returned


# ─── ava.skills.read() — explicit body-consumption API ────────────────────


def test_skills_read_consumes_and_records(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ava.skills.read(name)` returns the consumed shape (path line + body)
    and records the attribution — the explicit API equivalent of opening the
    proxy's `__doc__`. Names fold like everywhere else in the module."""
    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    d = fake_skills_dir / "grp"
    d.mkdir()
    _write_skill(d, "beta", "name: beta\ndescription: b", body="# B\n")

    recorded: list[tuple[str, str]] = []

    def capture(_db: Database, event: telemetry.Event) -> None:
        recorded.append((event.attributes["skill"], event.attributes["invocation_depth"]))

    monkeypatch.setattr("ava.sdk_surface.agent_identity.require_agent_id", _one_agent)
    monkeypatch.setattr("base.telemetry.audit_events.record_audit_reported", capture)

    out = skills_mod.read("alpha")
    assert out.endswith("# A\n")
    assert "alpha" in out  # path line present, __doc__ shape
    assert recorded == [("alpha", "loaded")]

    # Display identifier and its underscore/dot projection fold to one skill.
    assert skills_mod.read("grp:beta") == skills_mod.read("grp.beta")
    assert recorded[-1] == ("beta", "loaded")


def test_skills_read_returns_same_shape_as_proxy_doc(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read() and the proxy `__doc__` are the same consumption with the same
    return shape (path line + body), so an agent switching between them sees
    an identical payload."""
    import ava

    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")

    def _noop(_db: Database, _event: telemetry.Event) -> None:
        return None

    monkeypatch.setattr("ava.sdk_surface.agent_identity.require_agent_id", _one_agent)
    monkeypatch.setattr("base.telemetry.audit_events.record_audit_reported", _noop)
    assert skills_mod.read("alpha") == ava.skills.alpha.__doc__


def test_skills_read_unknown_name_raises(fake_skills_dir: Path) -> None:
    """An unknown name fails loud (ValueError naming it) rather than silently
    returning nothing — the same fail-fast stance as `ava.skills.<name>`."""
    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a")
    with pytest.raises(ValueError, match="no skill named"):
        skills_mod.read("does-not-exist")


def test_skills_read_rejects_non_string_name(fake_skills_dir: Path) -> None:
    """Argument validation matches the rest of the SDK: a non-string name
    raises TypeError, not a silent no-op. (A one-element string list is the
    documented trailing-comma unwrap and stays legal.)"""
    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a")
    with pytest.raises(TypeError):
        skills_mod.read(123)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        skills_mod.read(["alpha", "beta"])  # type: ignore[arg-type]


def test_help_skills_index_lists_read(
    fake_skills_dir: Path,
    capsys: pytest.CaptureFixture[str],
    model_installation: Installation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit API is discoverable: `ava.help(ava.skills)` renders `read`
    as a function entry (the surface list carries it), and `dir()` includes it
    — while index renders still record no attribution."""
    import ava

    monkeypatch.setattr(ava, "__plugin_installation__", model_installation, raising=False)

    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a")
    ava.help(ava.skills)
    out = capsys.readouterr().out
    assert "def read(" in out
    assert "read" in dir(ava.skills)


# ─── the event is written for real, and a failed write is loud ───────────────


def test_consuming_a_skill_lands_a_skill_invoked_row(
    fake_skills_dir: Path,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    """End to end with a real database: consuming a skill body leaves a
    `skill_invoked` row in `audit_events` for the consuming agent, carrying the
    `loaded` depth the self-evolution collector scores — one row per
    consumption, written at the call, with nothing held in this process."""
    from tests.fixtures.units import spawn_agent

    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    agent_id = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )

    def resolve_agent(_context: AvaContext | None = None) -> int:
        return agent_id

    monkeypatch.setattr("ava.sdk_surface.agent_identity.require_agent_id", resolve_agent)

    skills_mod.read("alpha")
    skills_mod.read("alpha")

    rows = db_conn.execute(
        "SELECT source, attributes FROM audit_events "
        "WHERE agent_id = %s AND event_name = 'skill_invoked'",
        (agent_id,),
    ).fetchall()
    assert (
        rows
        == [("self", {"skill": "alpha", "identifier": "alpha", "invocation_depth": "loaded"})] * 2
    )


def test_a_failed_write_is_reported_and_does_not_fail_the_read(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attribution must never take an agent down, and must never go quiet
    either: a failed `audit_events` write does not raise out of the read, and is
    reported through the audit module's loud path (error log with traceback plus
    an `audit_write_failed` anomaly event)."""
    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    monkeypatch.setattr("ava.sdk_surface.agent_identity.require_agent_id", _one_agent)

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("database down")

    monkeypatch.setattr("base.telemetry.audit_events.record_audit_standalone", _boom)

    errors: list[str] = []
    sink = logger.add(lambda m: errors.append(str(m)), level="ERROR")
    try:
        assert skills_mod.read("alpha").endswith("# A\n")  # the read itself succeeds
    finally:
        logger.remove(sink)

    (report,) = errors
    assert "skill_invoked could not be recorded" in report
    assert "RuntimeError: database down" in report  # the traceback rides along
