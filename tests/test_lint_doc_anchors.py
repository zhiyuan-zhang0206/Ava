from __future__ import annotations

import importlib

import pytest

_TARGET = """
CONST = 1
TYPED: int = 2


def top_level():
    pass


async def top_level_async():
    pass


class Holder:
    def method(self):
        def inner_helper():
            pass

        return inner_helper
"""


@pytest.fixture
def lint(monkeypatch, tmp_path):
    """A throwaway repo: both doc scan roots AND the resolution root.

    `_REPO_ROOT` is redirected too, so a case can write the target `.py` it
    anchors at. Leaving it pointed at the real repo would make every assertion
    here depend on whatever `shared/` happens to define today.
    """
    mod = importlib.import_module("scripts.lint_doc_anchors")
    docs = tmp_path / "conventions"
    docs.mkdir(parents=True)
    skills = tmp_path / ".agents" / "skills"
    skills.mkdir(parents=True)
    builtin = tmp_path / "ava_builtins" / "skills"
    builtin.mkdir(parents=True)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(_TARGET)
    monkeypatch.setattr(mod, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_DOCS_CONVENTIONS", docs)
    monkeypatch.setattr(mod, "_DEV_SKILLS", skills)
    monkeypatch.setattr(mod, "_BUILTIN_SKILLS", builtin)
    return mod, docs, skills, builtin


def test_dangling_symbol_is_flagged(lint):
    mod, docs, *_ = lint
    (docs / "a.md").write_text("The entry point is `pkg/mod.py:renamed_away`.\n")
    assert mod.check() == 1


def test_live_symbol_passes(lint):
    mod, docs, *_ = lint
    (docs / "a.md").write_text("The entry point is `pkg/mod.py:top_level`.\n")
    assert mod.check() == 0


def test_missing_target_file_is_flagged(lint):
    mod, docs, *_ = lint
    (docs / "a.md").write_text("See `pkg/deleted.py:top_level`.\n")
    assert mod.check() == 1


@pytest.mark.parametrize("symbol", ["top_level_async", "CONST", "TYPED", "Holder"])
def test_every_binding_form_resolves(lint, symbol):
    mod, docs, *_ = lint
    (docs / "a.md").write_text(f"See `pkg/mod.py:{symbol}`.\n")
    assert mod.check() == 0


@pytest.mark.parametrize("symbol", ["method", "inner_helper"])
def test_nested_symbol_resolves(lint, symbol):
    # A method / nested helper is a real symbol in that file. Demanding module
    # level would reject a legitimately documented one for no gain: a rename
    # removes the name at every depth, which is the drift class being guarded.
    mod, docs, *_ = lint
    (docs / "a.md").write_text(f"See `pkg/mod.py:{symbol}`.\n")
    assert mod.check() == 0


def test_dotted_anchor_validates_first_segment_only(lint):
    mod, docs, *_ = lint
    (docs / "ok.md").write_text("See `pkg/mod.py:Holder.method`.\n")
    assert mod.check() == 0
    # The deliberate half of the contract: the attribute is NOT resolved, so a
    # bogus one after a real first segment stays clean.
    (docs / "ok.md").write_text("See `pkg/mod.py:Holder.no_such_method`.\n")
    assert mod.check() == 0
    (docs / "ok.md").write_text("See `pkg/mod.py:Gone.method`.\n")
    assert mod.check() == 1


@pytest.mark.parametrize("prefix", ["", "./", "."])
def test_leading_dot_path_resolves_to_the_path_written(lint, prefix, tmp_path):
    # `.agents/skills/...` is the shape the module docstring holds up as
    # motivation. A regex anchored on `[A-Za-z_0-9]` starts matching after the
    # dot and reports `agents/...` missing — a path the author never wrote.
    mod, docs, *_ = lint
    if prefix == ".":
        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".hidden" / "mod.py").write_text(_TARGET)
        target = ".hidden/mod.py"
    else:
        target = f"{prefix}pkg/mod.py"
    (docs / "a.md").write_text(f"See `{target}:top_level`.\n")
    assert mod.check() == 0
    (docs / "a.md").write_text(f"See `{target}:renamed_away`.\n")
    assert mod.check() == 1


def test_imported_name_resolves(lint, tmp_path):
    # A re-export facade binds names it did not define; an anchor into one is
    # correct and must not be flagged.
    (tmp_path / "pkg" / "facade.py").write_text(
        "from pkg.mod import top_level\nimport pkg.mod as aliased\n"
    )
    mod, docs, *_ = lint
    (docs / "a.md").write_text("See `pkg/facade.py:top_level` and `pkg/facade.py:aliased`.\n")
    assert mod.check() == 0


def test_unparseable_target_is_a_violation_not_a_crash(lint, tmp_path):
    # The prototype did `except SyntaxError: continue`, silently dropping every
    # anchor into that file. Exploding with a bare traceback is no better — it
    # never names the citing doc. Report it.
    (tmp_path / "pkg" / "broken.py").write_text("def (((\n")
    mod, docs, *_ = lint
    (docs / "a.md").write_text("See `pkg/broken.py:anything`.\n")
    assert mod.check() == 1


def test_symlinked_skill_directory_is_descended(lint, tmp_path):
    # 26 of the 37 entries under the real `.agents/skills/` are symlinks to the
    # built-in skills. `Path.rglob` does not follow them, so most of that scan
    # root would be silently unscanned.
    mod, _, skills, _ = lint
    real = tmp_path / "builtin" / "some-skill"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("Read `pkg/mod.py:renamed_away` first.\n")
    (skills / "some-skill").symlink_to(real, target_is_directory=True)
    assert mod.check() == 1


def test_trailing_call_parens_do_not_hide_the_anchor(lint):
    # The shape that defeated the issue's prototype: it required the whole
    # backtick span to BE the anchor, so `...py:name()` was never checked. Three
    # live dangling anchors in runbook.md were hiding behind exactly this.
    mod, docs, *_ = lint
    (docs / "a.md").write_text("Composed via `pkg/mod.py:renamed_away()`.\n")
    assert mod.check() == 1


def test_prose_mention_outside_a_code_span_is_not_flagged(lint):
    mod, docs, *_ = lint
    (docs / "a.md").write_text("Historically pkg/mod.py:renamed_away did this.\n")
    assert mod.check() == 0


def test_pytest_node_id_is_not_an_anchor(lint):
    mod, docs, *_ = lint
    (docs / "a.md").write_text("Run `pytest pkg/mod.py::test_renamed_away -q`.\n")
    assert mod.check() == 0


def test_line_reference_is_not_an_anchor(lint):
    mod, docs, *_ = lint
    (docs / "a.md").write_text("The bug is at `pkg/mod.py:9999`.\n")
    assert mod.check() == 0


def test_dev_skill_doc_is_in_scope(lint):
    mod, _, skills, _ = lint
    (skills / "SKILL.md").write_text("Read `pkg/mod.py:renamed_away` first.\n")
    assert mod.check() == 1


def test_non_markdown_file_in_scope_is_scanned(lint):
    # `.agents/skills/ship-a-change/reference/ci_watcher.py` cites
    # `scripts/ci_utils.py:check_ci` in its docstring — an `.md`-only scan would
    # walk straight past it, which is how the `#65` rename lost `db/schema.sql`.
    mod, _, skills, _ = lint
    (skills / "helper.py").write_text('"""Wraps `pkg/mod.py:renamed_away`."""\n')
    assert mod.check() == 1


def test_okf_doc_is_in_scope(lint, tmp_path):
    # The OKF axis is the densest anchor surface and the whole point of #112.
    mod, *_ = lint
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "agent.ava.okf.md").write_text("Entry: `pkg/mod.py:renamed_away`.")
    assert mod.check() == 1
    (tmp_path / "agent" / "agent.ava.okf.md").write_text("Entry: `pkg/mod.py:top_level`.")
    assert mod.check() == 0


def test_okf_doc_relative_anchor_is_not_resolved(lint, tmp_path):
    # The OKF axis once wrote 15 doc-relative anchors; #112 normalised them
    # away rather than teaching the resolver a second meaning. A doc-relative
    # anchor is therefore flagged, even when the sibling file exists and binds
    # the symbol — the convention is repo-relative, one shape one meaning.
    mod, *_ = lint
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "_build.py").write_text(_TARGET)
    (tmp_path / "agent" / "agent.ava.okf.md").write_text("Entry: `_build.py:top_level`.")
    assert mod.check() == 1


def test_okf_anchor_with_trailing_call_parens_resolves(lint, tmp_path):
    # OKF anchors overwhelmingly carry `()` — `file.py:symbol()` — the shape
    # the anchor regex deliberately stops before.
    mod, *_ = lint
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "agent.ava.okf.md").write_text("Entry: `pkg/mod.py:top_level()`.")
    assert mod.check() == 0


def test_hidden_directory_okf_doc_is_not_scanned(lint, tmp_path):
    mod, *_ = lint
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "x.ava.okf.md").write_text("See `pkg/mod.py:renamed_away`.")
    assert mod.check() == 0


def test_github_okf_doc_is_scanned(lint, tmp_path):
    # `.github/` is the OKF walk's single hidden-dir exception, mirroring the
    # graph builder: it carries an overview node.
    mod, *_ = lint
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "x.ava.okf.md").write_text("See `pkg/mod.py:renamed_away`.")
    assert mod.check() == 1


def test_vendored_directories_are_not_scanned(lint, tmp_path):
    mod, *_ = lint
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "x.ava.okf.md").write_text("See `pkg/mod.py:renamed_away`.")
    assert mod.check() == 0


def test_builtin_skill_root_is_in_scope(lint, tmp_path):
    # The built-in skills are procedural like `.agents/skills/`; an agent reads
    # them as live instructions.
    mod, _, _, builtin = lint
    (builtin / "SKILL.md").write_text("Read `pkg/mod.py:renamed_away` first.")
    assert mod.check() == 1


def test_okf_doc_under_a_procedural_root_is_scanned_once(lint, tmp_path, capsys):
    # An OKF file under `ava_builtins/skills/` is in scope twice (procedural
    # root + OKF walk); it must be reported once, not twice.
    mod, _, _, builtin = lint
    (builtin / "sms.ava.okf.md").write_text("See `pkg/mod.py:renamed_away`.")
    assert mod.check() == 1
    err = capsys.readouterr().err
    assert err.count("sms.ava.okf.md:1:") == 1


def test_repo_tree_has_no_dangling_anchors():
    """The real scan, unpatched — the tree this lint gates must start green."""
    mod = importlib.import_module("scripts.lint_doc_anchors")
    assert mod.check() == 0
