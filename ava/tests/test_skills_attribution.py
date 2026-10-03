"""ava.skills attribution: the curated index render, SKILL.md read consumption (ava.help / ava.skills.read / files.read), and invocation recording gated on the write landing; split from ava/tests/test_skills.py (task #4922)."""

from pathlib import Path
from typing import Any

import pytest

import ava.skills as skills_mod
from ava.tests._skills_helpers import _overlay_all_enabled as _overlay_all_enabled
from ava.tests._skills_helpers import _write_skill
from ava.tests._skills_helpers import fake_skills_dir as fake_skills_dir

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
    fake_skills_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ava.help(ava.skills)` renders an INDEX: per entry its `ava.skills.<path>`
    heading plus its one-line description — never a SKILL.md body. The body is
    one `ava.help(ava.skills.<name>)` away; rendering it here would put the whole
    catalog into the prompt."""
    import ava

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
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mere access to a skill must not emit `skill_invoked`: `ava.skills.<name>`
    resolution, description reads, dir() and printing a proxy all record
    nothing — the old trigger fired on node resolution, so an agent that only
    glanced at the catalog (or named a skill in planning without ever opening
    it) claimed a fake "loaded" row (measured: ~70% of rows had no usage trace
    in the transcript). The signal fires on first SKILL.md body consumption —
    help() or a direct `__doc__` read — and the dedup keeps it one row per
    skill per run."""
    import ava

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
    monkeypatch.setattr(
        skills_mod,
        "_record_skill_invoked",
        lambda skill: recorded.append((skill["name"], "loaded")),  # pyright: ignore[reportUnknownArgumentType]
    )

    # Resolution and metadata reads — no body enters the conversation.
    leaf = ava.skills.alpha
    _ = leaf._description, leaf.path, leaf.name
    dir(leaf)
    repr(leaf)
    ns = ava.skills.guide
    _ = ns._description, ns.path
    dir(ns)
    assert recorded == []

    # First body consumption is the signal — and only once (dedup + cache).
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
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
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
    monkeypatch.setattr(
        skills_mod,
        "_record_skill_invoked",
        lambda skill: recorded.append((skill["name"], "loaded")),  # pyright: ignore[reportUnknownArgumentType]
    )

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
    monkeypatch.setattr(
        skills_mod,
        "_record_skill_invoked",
        lambda skill: recorded.append((skill["name"], "loaded")),  # pyright: ignore[reportUnknownArgumentType]
    )

    # The exact agent pattern: proxy.path + read of SKILL.md.
    out = ava.files.read(ava.skills.alpha.path + "/SKILL.md")
    assert out.endswith("# A\n") and "name: alpha" in out
    assert recorded == [("alpha", "loaded")]

    recorded.clear()
    # Range reads consume too — a partial body is still the body.
    ava.files.read(ava.skills.alpha.path + "/SKILL.md", start=1, end=1)
    assert recorded == [("alpha", "loaded")]


def test_files_read_other_files_do_not_record(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a loaded skill's own SKILL.md attributes: sibling files in the
    skill directory, a SKILL.md outside the mounted tree, and index renders
    must stay silent (a random SKILL.md is not a skill the agent loaded)."""
    import ava

    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    skill_dir = fake_skills_dir / "alpha"
    (skill_dir / "notes.md").write_text("# notes\n", encoding="utf-8")

    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        skills_mod,
        "_record_skill_invoked",
        lambda skill: recorded.append((skill["name"], "loaded")),  # pyright: ignore[reportUnknownArgumentType]
    )

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


def test_files_read_skill_md_attribution_deduped_per_run(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two files.read of the same SKILL.md in one agent run emit one row — the
    per-(agent, skill) dedup in `_record_skill_invoked` covers the direct-read
    path too, so repeated loads (re-reads during one turn) do not stack."""
    import ava

    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    monkeypatch.setattr(skills_mod, "_recorded_skill_invocations", set[tuple[int, str]]())
    monkeypatch.setattr("ava.agent_identity.require_agent_id", lambda: 1)

    attempts: list[int] = []

    def _ok(agent: int, skills: list[object]) -> bool:
        attempts.append(len(skills))
        return True

    monkeypatch.setattr(skills_mod, "_insert_skill_events", _ok)

    path = ava.skills.alpha.path + "/SKILL.md"
    ava.files.read(path)
    ava.files.read(path)
    ava.help(ava.skills.alpha)  # the same skill through the proxy — one row total
    assert attempts == [1]

    skills_mod.clear_recorded_skill_invocations()  # the public reset starts the run over
    ava.files.read(path)
    assert attempts == [1, 1]


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
    monkeypatch.setattr(
        skills_mod,
        "_record_skill_invoked",
        lambda skill: recorded.append((skill["name"], "loaded")),  # pyright: ignore[reportUnknownArgumentType]
    )

    out = skills_mod.read("alpha")
    assert out.endswith("# A\n")
    assert "alpha" in out  # path line present, __doc__ shape
    assert recorded == [("alpha", "loaded")]

    # Display identifier and its underscore/dot projection fold to one skill.
    assert skills_mod.read("grp:beta") == skills_mod.read("grp.beta")
    assert recorded[-1] == ("beta", "loaded")


def test_skills_read_deduped_across_spellings(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`read()` spelled three ways for one skill still writes ONE row — the
    per-(agent, skill) dedup, not the spelling, decides attribution."""
    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")
    monkeypatch.setattr(skills_mod, "_recorded_skill_invocations", set[tuple[int, str]]())
    monkeypatch.setattr("ava.agent_identity.require_agent_id", lambda: 1)

    attempts: list[int] = []

    def _ok(_agent: int, skills: list[object]) -> bool:
        attempts.append(len(skills))
        return True

    monkeypatch.setattr(skills_mod, "_insert_skill_events", _ok)

    skills_mod.read("alpha")
    skills_mod.read("alpha")  # same spelling — deduped
    assert attempts == [1]


def test_skills_read_returns_same_shape_as_proxy_doc(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read() and the proxy `__doc__` are the same consumption with the same
    return shape (path line + body), so an agent switching between them sees
    an identical payload."""
    import ava

    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a", body="# A\n")

    def _noop(_skill: object) -> None:
        return None

    monkeypatch.setattr(skills_mod, "_record_skill_invoked", _noop)
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
    fake_skills_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The explicit API is discoverable: `ava.help(ava.skills)` renders `read`
    as a function entry (the surface list carries it), and `dir()` includes it
    — while index renders still record no attribution."""
    import ava

    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a")
    ava.help(ava.skills)
    out = capsys.readouterr().out
    assert "def read(" in out
    assert "read" in dir(ava.skills)


# ─── attribution dedup is gated on the write landing ─────────────────────────


def test_a_failed_write_is_retried_not_remembered(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dedup set exists so a whole agent run emits one row per skill. Marking
    a skill recorded BEFORE the INSERT lands turns one swallowed DB blip into a
    permanent "already attributed" — and the write path swallows everything by
    design, so nothing downstream would ever notice. The set is updated only on a
    write that reported success, so the next access retries."""
    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a")
    monkeypatch.setattr(skills_mod, "_recorded_skill_invocations", set[tuple[int, str]]())
    monkeypatch.setattr("ava.agent_identity.require_agent_id", lambda: 1)

    attempts: list[int] = []

    def _failing(agent: int, skills: list[str]) -> bool:
        attempts.append(len(skills))
        return False

    monkeypatch.setattr(skills_mod, "_insert_skill_events", _failing)
    (skill,) = skills_mod.names()
    skills_mod._record_skill_invoked(skill)
    assert attempts == [1]
    assert skills_mod._recorded_skill_invocations == set()  # nothing remembered

    def _ok(agent: int, skills: list[str]) -> bool:
        attempts.append(len(skills))
        return True

    monkeypatch.setattr(skills_mod, "_insert_skill_events", _ok)
    skills_mod._record_skill_invoked(skill)
    assert attempts == [1, 1]  # retried, not skipped
    assert skills_mod._recorded_skill_invocations == {(1, "alpha")}

    skills_mod._record_skill_invoked(skill)
    assert attempts == [1, 1]  # now deduped — no third write


def test_a_swallowed_db_error_reports_failure(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_insert_skill_events` keeps swallowing (attribution must never take an
    agent down) but the caller has to be able to tell — otherwise the dedup gate
    above is gated on nothing. A failed `audit_events` write must surface as
    False so the dedup retries it."""
    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a")

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("database down")

    monkeypatch.setattr("base.telemetry.audit_events.record_audit_standalone_many", _boom)
    (skill,) = skills_mod.names()
    assert skills_mod._insert_skill_events(1, [skill]) is False


def test_insert_skill_events_writes_only_the_loaded_depth(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The producer has exactly one invocation depth left: `"loaded"`. The
    `prompt_injected` tier is gone (55K rows of baseline exposure drowned the
    real signal), so the payload the write path emits must never carry any
    other value — attribution consumers branch on this field."""
    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a")

    captured: list[dict[str, str]] = []

    def _capture(events: list[Any]) -> None:
        captured.extend(event.attributes for event in events)

    monkeypatch.setattr("base.telemetry.audit_events.record_audit_standalone_many", _capture)
    (skill,) = skills_mod.names()
    assert skills_mod._insert_skill_events(1, [skill]) is True
    assert captured == [{"skill": "alpha", "identifier": "alpha", "invocation_depth": "loaded"}]
