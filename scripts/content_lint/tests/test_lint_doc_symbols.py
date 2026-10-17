from __future__ import annotations

import pytest

from scripts.content_lint import lint_doc_symbols as mod

# Untyped fixtures and helper calls throughout: the call-site rules stay at warning for this file.
# pyright: reportUnknownMemberType = warning


@pytest.fixture
def lint(monkeypatch, tmp_path):
    """Point BOTH scan roots at throwaway dirs; valid_meiva_names() still reads the
    real live meiva namespace (that's the source of truth under test).

    Both roots are redirected, not just conventions — otherwise every case
    here would also scan the repo's real `.agents/skills/`, silently coupling these
    assertions to whatever a dev skill happens to reference."""
    docs = tmp_path / "docs" / "conventions"
    docs.mkdir(parents=True)
    skills = tmp_path / ".meiva" / "skills"
    skills.mkdir(parents=True)
    monkeypatch.setattr(mod, "DOCS_CONVENTIONS", docs)
    monkeypatch.setattr(mod, "DEV_SKILLS", skills)
    return mod, docs


@pytest.fixture
def dev_skills(monkeypatch, tmp_path):
    """The `.agents/skills/` scan root, isolated; conventions pointed at an empty dir."""
    docs = tmp_path / "docs" / "conventions"
    docs.mkdir(parents=True)
    skills = tmp_path / ".meiva" / "skills" / "example-project-skill"
    skills.mkdir(parents=True)
    monkeypatch.setattr(mod, "DOCS_CONVENTIONS", docs)
    monkeypatch.setattr(mod, "DEV_SKILLS", tmp_path / ".meiva" / "skills")
    return mod, skills


def test_dev_skill_fake_ref_is_flagged(dev_skills):
    mod, skills = dev_skills
    # A dev skill is a procedural instruction an agent follows now, so a dangling
    # meiva.* in it is the same drift class conventions is gated for.
    (skills / "SKILL.md").write_text("Read it via `meiva.bogus.read`.\n")
    assert mod.check() == 1


def test_dev_skill_valid_ref_passes(dev_skills):
    mod, skills = dev_skills
    (skills / "SKILL.md").write_text("Read it via `meiva.files.read`.\n")
    assert mod.check() == 0


def test_valid_ref_in_code_span_passes(lint):
    mod, docs = lint
    # meiva.watcher is a real submodule; in a backtick span -> clean.
    (docs / "a.md").write_text("The cron surface lives in `meiva.watcher.cron`.\n")
    assert mod.check() == 0


def test_fake_ref_in_code_span_is_flagged(lint):
    mod, docs = lint
    (docs / "a.md").write_text("Call `meiva.bogus.frobnicate()` to do the thing.\n")
    assert mod.check() == 1


def test_fake_ref_in_fenced_block_is_flagged(lint):
    mod, docs = lint
    (docs / "a.md").write_text("```python\nmeiva.bogus.run()\n```\n")
    assert mod.check() == 1


def test_prose_only_mention_not_flagged(lint):
    mod, docs = lint
    # Bare prose 'meiva.bogus' with NO backticks -> the FP guard means it is not
    # checked. This is the load-bearing zero-false-positive case.
    (docs / "a.md").write_text("Historically the meiva.bogus module did things.\n")
    assert mod.check() == 0


def test_hostname_suffix_not_flagged(lint):
    mod, docs = lint
    # `meiva.example.com` is a hostname, not a symbol -> domain guard skips it.
    (docs / "a.md").write_text("Reach the gateway at `meiva.example.com`.\n")
    assert mod.check() == 0


def test_metasyntactic_placeholder_not_flagged(lint):
    mod, docs = lint
    # `help(meiva.X)` is the docstring stand-in for "any submodule" -> meta guard skips it.
    (docs / "a.md").write_text("Drill into a submodule with `help(meiva.X)`.\n")
    assert mod.check() == 0


def test_dunder_attr_not_flagged(lint):
    mod, docs = lint
    # `meiva.__all_for_meiva__` is Python protocol surface the design docs cite, not a
    # deletable SDK member -> dunder guard skips it.
    (docs / "a.md").write_text("The agent-facing surface is `meiva.__all_for_meiva__`.\n")
    assert mod.check() == 0


def test_plugin_namespace_is_valid(lint):
    mod, docs = lint
    # `cwd` is declared by meiva_code as an `SdkNamespace` -> a live meiva.* member,
    # discovered by the plugin-scan half of valid_meiva_names().
    assert "cwd" in mod.valid_meiva_names()
    (docs / "a.md").write_text("Set the working dir with `meiva.cwd.set(p)`.\n")
    assert mod.check() == 0


def test_exemption_path(lint, monkeypatch):
    mod, docs = lint
    (docs / "a.md").write_text("A special `meiva.legacyshim` reference.\n")
    assert mod.check() == 1  # not a member -> flagged
    monkeypatch.setattr(mod, "_EXEMPT", {"legacyshim"})
    assert mod.check() == 0  # exempted -> clean


def test_referenced_symbols_helper():
    md = (
        "prose meiva.notchecked here\n"
        "inline `meiva.shell.list` and `meiva.example.com` and `help(meiva.X)`\n"
        "```\nmeiva.watcher.cron\n```\n"
    )
    syms = mod.referenced_symbols(md)
    assert "shell" in syms  # inline code span
    assert "watcher" in syms  # fenced block
    assert "notchecked" not in syms  # bare prose -> excluded
    assert "example" not in syms  # hostname -> excluded
    assert "X" not in syms  # metasyntactic -> excluded
