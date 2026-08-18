from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def lint(monkeypatch, tmp_path):
    """Point BOTH scan roots at throwaway dirs; valid_ava_names() still reads the
    real live ava namespace (that's the source of truth under test).

    Both roots are redirected, not just conventions — otherwise every case
    here would also scan the repo's real `.agents/skills/`, silently coupling these
    assertions to whatever a dev skill happens to reference."""
    mod = importlib.import_module("scripts.lint_doc_symbols")
    docs = tmp_path / "conventions"
    docs.mkdir(parents=True)
    skills = tmp_path / ".ava" / "skills"
    skills.mkdir(parents=True)
    monkeypatch.setattr(mod, "_DOCS_CONVENTIONS", docs)
    monkeypatch.setattr(mod, "_DEV_SKILLS", skills)
    return mod, docs


@pytest.fixture
def dev_skills(monkeypatch, tmp_path):
    """The `.agents/skills/` scan root, isolated; conventions pointed at an empty dir."""
    mod = importlib.import_module("scripts.lint_doc_symbols")
    docs = tmp_path / "conventions"
    docs.mkdir(parents=True)
    skills = tmp_path / ".ava" / "skills" / "ava-sweeper"
    skills.mkdir(parents=True)
    monkeypatch.setattr(mod, "_DOCS_CONVENTIONS", docs)
    monkeypatch.setattr(mod, "_DEV_SKILLS", tmp_path / ".ava" / "skills")
    return mod, skills


def test_dev_skill_fake_ref_is_flagged(dev_skills):
    mod, skills = dev_skills
    # A dev skill is a procedural instruction an agent follows now, so a dangling
    # ava.* in it is the same drift class conventions is gated for.
    (skills / "SKILL.md").write_text("Read it via `ava.bogus.read`.\n")
    assert mod.check() == 1


def test_dev_skill_valid_ref_passes(dev_skills):
    mod, skills = dev_skills
    (skills / "SKILL.md").write_text("Read it via `ava.files.read`.\n")
    assert mod.check() == 0


def test_valid_ref_in_code_span_passes(lint):
    mod, docs = lint
    # ava.watcher is a real submodule; in a backtick span -> clean.
    (docs / "a.md").write_text("The cron surface lives in `ava.watcher.cron`.\n")
    assert mod.check() == 0


def test_fake_ref_in_code_span_is_flagged(lint):
    mod, docs = lint
    (docs / "a.md").write_text("Call `ava.bogus.frobnicate()` to do the thing.\n")
    assert mod.check() == 1


def test_fake_ref_in_fenced_block_is_flagged(lint):
    mod, docs = lint
    (docs / "a.md").write_text("```python\nava.bogus.run()\n```\n")
    assert mod.check() == 1


def test_prose_only_mention_not_flagged(lint):
    mod, docs = lint
    # Bare prose 'ava.bogus' with NO backticks -> the FP guard means it is not
    # checked. This is the load-bearing zero-false-positive case.
    (docs / "a.md").write_text("Historically the ava.bogus module did things.\n")
    assert mod.check() == 0


def test_hostname_suffix_not_flagged(lint):
    mod, docs = lint
    # `ava.example.com` is a hostname, not a symbol -> domain guard skips it.
    (docs / "a.md").write_text("Reach the gateway at `ava.example.com`.\n")
    assert mod.check() == 0


def test_metasyntactic_placeholder_not_flagged(lint):
    mod, docs = lint
    # `help(ava.X)` is the docstring stand-in for "any submodule" -> meta guard skips it.
    (docs / "a.md").write_text("Drill into a submodule with `help(ava.X)`.\n")
    assert mod.check() == 0


def test_dunder_attr_not_flagged(lint):
    mod, docs = lint
    # `ava.__all_for_ava__` is Python protocol surface the design docs cite, not a
    # deletable SDK member -> dunder guard skips it.
    (docs / "a.md").write_text("The agent-facing surface is `ava.__all_for_ava__`.\n")
    assert mod.check() == 0


def test_plugin_namespace_is_valid(lint):
    mod, docs = lint
    # `cwd` is registered by ava_code via register_namespace -> a live ava.* member,
    # discovered by the plugin-scan half of valid_ava_names().
    assert "cwd" in mod.valid_ava_names()
    (docs / "a.md").write_text("Set the working dir with `ava.cwd.set(p)`.\n")
    assert mod.check() == 0


def test_exemption_path(lint, monkeypatch):
    mod, docs = lint
    (docs / "a.md").write_text("A special `ava.legacyshim` reference.\n")
    assert mod.check() == 1  # not a member -> flagged
    monkeypatch.setattr(mod, "_EXEMPT", {"legacyshim"})
    assert mod.check() == 0  # exempted -> clean


def test_referenced_symbols_helper():
    mod = importlib.import_module("scripts.lint_doc_symbols")
    md = (
        "prose ava.notchecked here\n"
        "inline `ava.shell.list` and `ava.example.com` and `help(ava.X)`\n"
        "```\nava.watcher.cron\n```\n"
    )
    syms = mod.referenced_symbols(md)
    assert "shell" in syms  # inline code span
    assert "watcher" in syms  # fenced block
    assert "notchecked" not in syms  # bare prose -> excluded
    assert "example" not in syms  # hostname -> excluded
    assert "X" not in syms  # metasyntactic -> excluded
