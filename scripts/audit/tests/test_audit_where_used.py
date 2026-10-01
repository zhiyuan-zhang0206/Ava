"""where_used finds every reference to a symbol, module or file and groups it by what it is."""

import json
from typing import Any, cast

import pytest

from scripts.audit import where_used as gate
from scripts.audit import where_used_scan as scan

FILES = {
    "pkg/__init__.py": "from .engine import run as run\n",
    "pkg/engine.py": (
        '"""The engine; start it with `python -m pkg.engine`."""\n\n\n'
        "def run() -> None: ...\n\n\nclass Motor: ...\n"
    ),
    "pkg/helpers.py": (
        '"""Helpers around pkg.engine."""\nfrom . import engine\nfrom .engine import Motor\n\n\n'
        "def go() -> None:\n    engine.run()\n    # restart pkg.engine here\n"
    ),
    "pkg/sub/__init__.py": "",
    "pkg/sub/deep.py": "from .. import engine\nfrom ..engine import run\n",
    "pkg/tests/test_engine_extra.py": "from pkg.engine import Motor\n",
    "pkg/docs/pkg.ava.okf.md": "The engine lives in `pkg/engine.py`.\n",
    "app/main.py": (
        "from pkg import run\nimport pkg.engine as eng\n\n\n"
        "def f() -> None:\n    from pkg.engine import Motor\n    eng.run()\n    run()\n"
    ),
    "app/other.py": "import pkg.engine as e\n\nm = e.Motor()\n",
    "app/star.py": "from pkg.engine import *\n",
    "app/loader.py": (
        'import importlib\n\nmod = importlib.import_module("pkg.engine")\n'
        'TABLE = {"motor": "pkg.engine.Motor"}\n'
    ),
    "tests/test_engine.py": (
        "import pkg.engine\n\n\ndef test_it(monkeypatch):\n"
        '    monkeypatch.setattr("pkg.engine.run", lambda: None)\n'
    ),
    "tests/test_engine_cli.py": "def test_cli() -> None: ...\n",
    "docs/guide.md": (
        "Start with [the engine](../pkg/engine.py).\n"
        "Call `run` to start it.\n"
        "Plain run in prose is not a mention.\n"
    ),
    "docs/unrelated.md": "Call `run` in your shell. A `Motor` here is not an engine.\n",
    "notes.md": "engine.py is where the loop is.\n",
    "decisions/2026-01-01-engine.md": "We moved pkg.engine here.\n",
    "CHANGELOG.md": "- pkg/engine.py was added.\n",
    "scripts/structure/baseline/pkg.json": '{"complexity": {"pkg/engine.py::run": 12}}\n',
    "pyproject.toml": '[tool.x]\nfiles = ["pkg/engine.py"]\n',
    "serve.sh": "python -m pkg.engine --flag\n",
}


def _report(raw: str, files: dict[str, str] | None = None) -> gate.Report:
    repo = scan.index_repo(FILES if files is None else files)
    return gate.scan(repo, scan.resolve(raw, repo))


def _kinds(report: gate.Report, group: str) -> dict[tuple[str, int], str]:
    return {(hit.path, hit.line): hit.kind for hit in report.groups[group]}


def _paths(report: gate.Report, group: str) -> set[str]:
    return {hit.path for hit in report.groups[group]}


def test_module_imports_cover_absolute_relative_deferred_and_wildcard() -> None:
    kinds = _kinds(_report("pkg.engine"), "imports")

    assert kinds == {
        ("app/main.py", 2): "import",
        ("app/main.py", 6): "import in function",
        ("app/other.py", 1): "import",
        ("app/star.py", 1): "wildcard import",
        ("pkg/__init__.py", 1): "import",
        ("pkg/helpers.py", 2): "import",
        ("pkg/helpers.py", 3): "import",
        ("pkg/sub/deep.py", 1): "import",
        ("pkg/sub/deep.py", 2): "import",
    }


def test_symbol_follows_the_package_door_and_attribute_use_but_not_other_names() -> None:
    report = _report("pkg.engine:run")

    assert [(d.module, d.name, d.where) for d in report.doors] == [
        ("pkg", "run", "pkg/__init__.py:1")
    ]
    assert _kinds(report, "imports") == {
        ("app/main.py", 1): "import",  # `from pkg import run` reaches it through the door
        ("app/main.py", 7): "attribute",  # `eng.run()` through `import pkg.engine as eng`
        ("app/star.py", 1): "wildcard import",
        ("pkg/__init__.py", 1): "re-export",
        ("pkg/helpers.py", 7): "attribute",  # `engine.run()` through `from . import engine`
        ("pkg/sub/deep.py", 2): "import",
    }
    assert "app/other.py" not in _paths(report, "imports")  # imports the module, uses `Motor`


def test_symbol_forms_name_it_in_every_spelling() -> None:
    for raw in ("pkg.engine:run", "pkg.engine.run", "pkg/engine.py::run", "pkg/engine.py:run"):
        repo = scan.index_repo(FILES)
        target = scan.resolve(raw, repo)
        assert (target.kind, target.module, target.name, target.defined_at) == (
            "symbol",
            "pkg.engine",
            "run",
            "pkg/engine.py:4",
        )


def test_strings_separate_patch_targets_dynamic_imports_entry_points_and_registries() -> None:
    report = _report("pkg.engine")

    assert _kinds(report, "strings") == {
        ("app/loader.py", 3): "dynamic import",
        ("app/loader.py", 4): "string",
        ("serve.sh", 1): "module entry",
    }
    assert _kinds(report, "tests")[("tests/test_engine.py", 5)] == "patch target"


def test_tests_show_where_they_live_and_which_are_only_named_for_the_module() -> None:
    report = _report("pkg.engine")

    assert _kinds(report, "tests") == {
        ("pkg/tests/test_engine_extra.py", 1): "import",
        ("tests/test_engine.py", 1): "import",
        ("tests/test_engine.py", 5): "patch target",
        ("tests/test_engine_cli.py", 0): "name match",
    }
    header = next(
        line for line in gate.render(report, None).splitlines() if line.startswith("TESTS")
    )
    assert "3 files" in header
    assert "1 beside the code, 2 in top-level tests/" in header


def test_docs_resolve_links_and_match_file_names_docstrings_and_comments() -> None:
    kinds = _kinds(_report("pkg.engine"), "docs")

    assert kinds == {
        ("docs/guide.md", 1): "link",
        ("notes.md", 1): "mention (file name)",
        ("pkg/docs/pkg.ava.okf.md", 1): "mention",
        ("pkg/helpers.py", 1): "docstring",
        ("pkg/helpers.py", 8): "comment",
    }


def test_a_symbol_is_a_mention_only_inside_inline_code() -> None:
    kinds = _kinds(_report("pkg.engine:run"), "docs")

    assert kinds == {("docs/guide.md", 2): "mention (bare name)"}


def test_a_plain_word_symbol_is_a_mention_only_in_docs_about_its_module() -> None:
    plain = _kinds(_report("pkg.engine:run"), "docs")
    distinctive = _kinds(_report("pkg.engine:Motor"), "docs")

    assert ("docs/unrelated.md", 1) not in plain  # `run` here is an ordinary word
    assert distinctive == {("docs/unrelated.md", 1): "mention (bare name)"}  # `Motor` is not


def test_structure_registrations_and_frozen_history_are_their_own_groups() -> None:
    report = _report("pkg.engine")

    assert _paths(report, "structure") == {"pyproject.toml", "scripts/structure/baseline/pkg.json"}
    assert _paths(report, "history") == {"decisions/2026-01-01-engine.md", "CHANGELOG.md"}
    assert "CHANGELOG.md" not in _paths(report, "docs")
    symbol = _report("pkg.engine:run")
    assert _paths(symbol, "structure") == {"scripts/structure/baseline/pkg.json"}


def test_a_package_hides_its_own_modules_but_not_its_tests_and_docs() -> None:
    report = _report("pkg")

    assert _paths(report, "imports") == {"app/main.py", "app/other.py", "app/star.py"}
    assert _paths(report, "tests") >= {"pkg/tests/test_engine_extra.py", "tests/test_engine.py"}
    assert "pkg/docs/pkg.ava.okf.md" in _paths(report, "docs")
    assert report.hidden > 0
    assert not any(
        path.startswith("pkg/") and path.endswith(".py") for path in _paths(report, "imports")
    )


def test_a_longer_path_or_dotted_name_is_not_a_reference() -> None:
    files = {
        "pkg/engine.py": "def run() -> None: ...\n",
        "other/pkg/engine.py": "def run() -> None: ...\n",
        "notes.txt": "x.pkg.engine and other/pkg/engine.py and pkg/engine.pyc and pkg/engine_x\n",
        "real.txt": "see pkg/engine.py and ./pkg/engine.py\n",
    }

    report = _report("pkg/engine.py", files)

    assert _paths(report, "strings") == {"real.txt"}
    assert [hit.line for hit in report.groups["strings"]] == [1]


def test_object_form_patches_of_a_symbol_are_patch_targets_but_other_names_are_not() -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/engine.py": "def run() -> None: ...\n\n\ndef stop() -> None: ...\n",
        "tests/test_a.py": (
            "import pkg.engine as eng\nfrom unittest import mock\n\n\n"
            "def test_it(monkeypatch):\n"
            '    monkeypatch.setattr(eng, "run", lambda: None)\n'
            '    with mock.patch.object(eng, "run"):\n        pass\n'
            '    monkeypatch.setattr(eng, "stop", lambda: None)\n'
        ),
        "tests/test_b.py": (
            "from pkg import engine\n\n\ndef test_it(monkeypatch):\n"
            '    monkeypatch.setattr(engine, "run", lambda: None)\n'
        ),
    }

    kinds = _kinds(_report("pkg.engine:run", files), "tests")

    assert kinds == {
        ("tests/test_a.py", 6): "patch target",
        ("tests/test_a.py", 7): "patch target",
        ("tests/test_b.py", 5): "patch target",
    }


def test_a_script_is_found_by_its_unique_file_name_in_docs_and_path_literals() -> None:
    files = {
        "scripts/gen-page.py": "print('page')\n",
        "scripts/tests/test_gen.py": 'SCRIPT = ROOT / "scripts" / "gen-page.py"\n',
        "docs/generators.md": "Run `gen-page.py` to refresh the page.\n",
        "notes/other.md": "Nothing about it here.\n",
    }

    report = _report("scripts/gen-page.py", files)

    assert _kinds(report, "tests") == {("scripts/tests/test_gen.py", 1): "string"}
    assert _kinds(report, "docs") == {("docs/generators.md", 1): "mention (file name)"}


def test_a_deferred_import_in_a_package_init_is_not_a_re_export() -> None:
    files = {
        "pkg/engine.py": "def run() -> None: ...\n",
        "pkg/__init__.py": "def go() -> None:\n    from .engine import run\n    run()\n",
        "app/use.py": "from pkg import run\n",
    }

    report = _report("pkg.engine:run", files)

    assert report.doors == ()
    assert _kinds(report, "imports") == {("pkg/__init__.py", 2): "import in function"}


def test_resolution_accepts_modules_packages_files_and_directories() -> None:
    repo = scan.index_repo(FILES)

    assert scan.resolve("./pkg//engine.py", repo).path == "pkg/engine.py"
    assert scan.resolve("pkg/", repo).path == "pkg"

    assert scan.resolve("pkg/engine.py", repo).kind == "module"
    assert scan.resolve("pkg.sub", repo).kind == "package"
    assert scan.resolve("pkg", repo).path == "pkg"
    assert scan.resolve("docs/guide.md", repo).kind == "file"
    assert scan.resolve("docs", repo).kind == "directory"
    assert scan.resolve("app", repo).kind == "package"  # no __init__.py: a namespace package


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("pkg.engine:nope", "binds no top-level name 'nope'"),
        ("pkg.engine.nope", "binds no top-level name 'nope'"),
        ("missing.mod:run", "is not a module or Python file"),
        ("no/such/path.md", "is not a symbol, module, tracked file or directory"),
        ("app:run", "namespace package"),
        ("pkg.engine:Motor.start", "name the class or function, not a member"),
    ],
)
def test_resolution_fails_fast_on_unknown_targets(raw: str, message: str) -> None:
    repo = scan.index_repo(FILES)

    with pytest.raises(ValueError, match=message):
        scan.resolve(raw, repo)


def test_output_caps_files_per_group_but_json_and_all_are_complete() -> None:
    files = {"pkg/engine.py": "def run() -> None: ...\n"}
    files.update({f"user{i:02d}/use.py": "import pkg.engine\n" for i in range(15)})
    report = _report("pkg.engine", files)

    capped = gate.render(report, 3).splitlines()
    full = gate.render(report, None)

    assert sum(line.startswith("  user") for line in capped) == 3
    assert any("12 more files" in line and "user" in line for line in capped)
    assert full.count("use.py:1") == 15
    groups = cast("dict[str, dict[str, Any]]", gate.as_json(report)["groups"])
    assert groups["imports"]["files"] == 15
    assert len(groups["imports"]["hits"]) == 15


def test_main_reports_a_real_symbol_as_json_and_exits_2_on_an_unknown_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert gate.main(["scripts.audit.module_moves:is_frozen", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    imports = {hit["path"] for hit in payload[0]["groups"]["imports"]["hits"]}

    assert payload[0]["target"]["name"] == "is_frozen"
    assert "scripts/audit/where_used.py" in imports

    with pytest.raises(SystemExit) as exit_info:
        gate.main(["scripts.audit.module_moves:no_such_name"])
    assert exit_info.value.code == 2


def test_an_absolute_path_inside_this_checkout_is_made_relative() -> None:
    absolute = str(gate._REPO / "scripts" / "audit" / "where_used.py")

    assert gate._relative(absolute) == "scripts/audit/where_used.py"
    assert gate._relative(absolute + "::scan") == "scripts/audit/where_used.py::scan"
    assert gate._relative("/elsewhere/x.py") == "/elsewhere/x.py"
    assert gate._relative("pkg.mod:name") == "pkg.mod:name"
