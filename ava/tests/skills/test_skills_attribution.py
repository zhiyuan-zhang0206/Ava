from pathlib import Path
from typing import Any

import psycopg
import pytest

import ava.skills as skills_mod
from ava.sdk_surface import agent_identity
from ava.sdk_surface.installation import Installation
from ava.tests.skills._skills_helpers import fake_skills_dir as fake_skills_dir
from ava.tests.skills._skills_helpers import overlay_all_enabled as overlay_all_enabled
from ava.tests.skills._skills_helpers import write_skill
from base import telemetry
from base.agents.context import AvaContext
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.log import logger
from base.telemetry import audit_events


def _one_agent(_context: AvaContext | None = None) -> int:
    return 1


pytestmark = pytest.mark.usefixtures("unit_home")


def test_all_for_ava_is_the_live_top_level_index(fake_skills_dir: Path) -> None:
    import ava

    write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a")
    d = fake_skills_dir / "grp"
    d.mkdir()
    write_skill(d, "beta", "name: beta\ndescription: b")

    assert skills_mod.__all_for_ava__ == ["alpha", "grp"]
    assert ava.agent_visible_names(skills_mod) == ["alpha", "grp"]


def test_help_on_skills_module_is_index_only(
    fake_skills_dir: Path,
    capsys: pytest.CaptureFixture[str],
    model_installation: Installation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ava

    monkeypatch.setattr(ava, "__plugin_installation__", model_installation, raising=False)

    write_skill(
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
    import ava

    monkeypatch.setattr(ava, "__plugin_installation__", model_installation, raising=False)

    write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    d = fake_skills_dir / "grp"
    d.mkdir()
    write_skill(d, "beta", "name: beta\ndescription: b", body="# B\n")
    root = fake_skills_dir / "guide"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: guide\ndescription: g\n---\n\n# Guide body\n", encoding="utf-8"
    )
    write_skill(root, "sub", "name: sub\ndescription: s", body="# Sub\n")

    recorded: list[tuple[str, str]] = []

    def capture(_db: Database, event: telemetry.Event) -> None:
        recorded.append((event.attributes["skill"], event.attributes["invocation_depth"]))

    monkeypatch.setattr(agent_identity, "require_agent_id", _one_agent)
    monkeypatch.setattr(audit_events, "record_audit_reported", capture)

    leaf = ava.skills.alpha
    _ = leaf._description, leaf.path, leaf.name
    dir(leaf)
    repr(leaf)
    ns = ava.skills.guide
    _ = ns._description, ns.path
    dir(ns)
    assert recorded == []

    _ = leaf.__doc__
    assert [n for n, _d in recorded] == ["alpha"]
    _ = leaf.__doc__
    assert [n for n, _d in recorded] == ["alpha"]

    recorded.clear()
    ava.help(ava.skills.guide)
    assert [n for n, _d in recorded] == ["guide"]


def test_index_render_records_no_loaded_attribution(
    fake_skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_installation: Installation,
) -> None:
    import ava

    monkeypatch.setattr(ava, "__plugin_installation__", model_installation, raising=False)

    write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    d = fake_skills_dir / "grp"
    d.mkdir()
    write_skill(d, "beta", "name: beta\ndescription: b", body="# B\n")
    root = fake_skills_dir / "guide"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: guide\ndescription: g\n---\n\n# Guide body\n", encoding="utf-8"
    )
    write_skill(root, "sub", "name: sub\ndescription: s", body="# Sub\n")

    recorded: list[tuple[str, str]] = []

    def capture(_db: Database, event: telemetry.Event) -> None:
        recorded.append((event.attributes["skill"], event.attributes["invocation_depth"]))

    monkeypatch.setattr(agent_identity, "require_agent_id", _one_agent)
    monkeypatch.setattr(audit_events, "record_audit_reported", capture)

    ava.help(ava.skills)
    assert recorded == []
    ava.help(ava.skills.grp)
    assert recorded == []
    ava.help(ava.skills.guide)
    assert [n for n, _d in recorded] == ["guide"]

    recorded.clear()
    ava.help(ava.skills.alpha)
    assert recorded == [("alpha", "loaded")]


def test_files_read_skill_md_records_consumption(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ava

    write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    skill_dir = fake_skills_dir / "alpha"
    (skill_dir / "notes.md").write_text("# notes\n", encoding="utf-8")

    recorded: list[tuple[str, str]] = []

    def capture(_db: Database, event: telemetry.Event) -> None:
        recorded.append((event.attributes["skill"], event.attributes["invocation_depth"]))

    monkeypatch.setattr(agent_identity, "require_agent_id", _one_agent)
    monkeypatch.setattr(audit_events, "record_audit_reported", capture)

    out = ava.files.read(ava.skills.alpha.path + "/SKILL.md")
    assert out.endswith("# A\n") and "name: alpha" in out
    assert recorded == [("alpha", "loaded")]

    recorded.clear()
    ava.files.read(ava.skills.alpha.path + "/SKILL.md", start=1, end=1)
    assert recorded == [("alpha", "loaded")]


def test_files_read_other_files_do_not_record(
    fake_skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_installation: Installation,
) -> None:
    import ava

    monkeypatch.setattr(ava, "__plugin_installation__", model_installation, raising=False)

    write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    skill_dir = fake_skills_dir / "alpha"
    (skill_dir / "notes.md").write_text("# notes\n", encoding="utf-8")

    recorded: list[tuple[str, str]] = []

    def capture(_db: Database, event: telemetry.Event) -> None:
        recorded.append((event.attributes["skill"], event.attributes["invocation_depth"]))

    monkeypatch.setattr(agent_identity, "require_agent_id", _one_agent)
    monkeypatch.setattr(audit_events, "record_audit_reported", capture)

    ava.files.read(str(skill_dir / "notes.md"))
    assert recorded == []

    stray = fake_skills_dir.parent / "stray"
    stray.mkdir()
    (stray / "SKILL.md").write_text("---\nname: stray\ndescription: s\n---\n", encoding="utf-8")
    ava.files.read(str(stray / "SKILL.md"))
    assert recorded == []

    ava.help(ava.skills)
    assert recorded == []


def test_every_consumption_records_one_event_and_nothing_is_remembered(
    fake_skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_installation: Installation,
) -> None:
    import ava

    monkeypatch.setattr(ava, "__plugin_installation__", model_installation, raising=False)

    write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    monkeypatch.setattr(agent_identity, "require_agent_id", _one_agent)

    written: list[Any] = []

    def _capture(_db: object, event: Any) -> None:
        written.append(event)

    monkeypatch.setattr(audit_events, "record_audit_reported", _capture)

    path = ava.skills.alpha.path + "/SKILL.md"
    ava.files.read(path)
    ava.files.read(path)
    ava.help(ava.skills.alpha)
    assert [e.attributes["skill"] for e in written] == ["alpha"] * 3
    assert not hasattr(skills_mod, "_recorded_skill_invocations")


def test_files_read_skill_md_silent_outside_agent(db_url: str, fake_skills_dir: Path) -> None:
    import ava

    write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    out = ava.files.read(ava.skills.alpha.path + "/SKILL.md")
    assert out.endswith("# A\n")


def test_proxy_doc_consumes_and_records(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ava

    write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    d = fake_skills_dir / "grp"
    d.mkdir()
    write_skill(d, "beta", "name: beta\ndescription: b", body="# B\n")

    recorded: list[tuple[str, str]] = []

    def capture(_db: Database, event: telemetry.Event) -> None:
        recorded.append((event.attributes["skill"], event.attributes["invocation_depth"]))

    monkeypatch.setattr(agent_identity, "require_agent_id", _one_agent)
    monkeypatch.setattr(audit_events, "record_audit_reported", capture)

    out = ava.skills.alpha.__doc__
    assert out is not None
    assert out.endswith("# A\n")
    assert "alpha" in out
    assert recorded == [("alpha", "loaded")]

    nested = ava.skills.grp.beta.__doc__
    assert nested is not None
    assert nested.endswith("# B\n")
    assert recorded[-1] == ("beta", "loaded")


def test_consuming_a_skill_lands_a_skill_invoked_row(
    redis_url: str,
    fake_skills_dir: Path,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    from base.agents.tests.units import spawn_agent

    write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )

    def resolve_agent(_context: AvaContext | None = None) -> int:
        return agent_id

    monkeypatch.setattr(agent_identity, "require_agent_id", resolve_agent)

    import ava

    assert ava.skills.alpha.__doc__
    assert ava.skills.alpha.__doc__

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
    import ava

    write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    monkeypatch.setattr(agent_identity, "require_agent_id", _one_agent)

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("database down")

    monkeypatch.setattr(audit_events, "record_audit_standalone", _boom)

    errors: list[str] = []
    sink = logger.add(lambda m: errors.append(str(m)), level="ERROR")
    try:
        body = ava.skills.alpha.__doc__
        assert body is not None
        assert body.endswith("# A\n")
    finally:
        logger.remove(sink)

    (report,) = errors
    assert "skill_invoked could not be recorded" in report
    assert "RuntimeError: database down" in report
