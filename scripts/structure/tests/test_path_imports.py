"""Rule 6: code under ava_builtins/ reaches other code through packages, never a file path.

Unit coverage of `path_imports.measure` (which forms count, which do not, and the
ava_builtins/ scope), then the gate lifecycle through lcs.main(): a new site
fails, a frozen one passes, a fixed one fails as stale, and the base-revision
guard refuses any new key once it can actually compare against a real base.
"""

from __future__ import annotations

import ast

import pytest

from scripts.structure import path_imports

_SKILL = "ava_builtins/skills/demo/reference/run.py"


def _sites(source: str, rel_path: str = _SKILL) -> dict[str, list[int]]:
    return path_imports.measure(ast.parse(source), rel_path)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("import sys\nsys.path.insert(0, 'x')\n", "sys.path"),
        ("import sys\nsys.path.append('x')\n", "sys.path"),
        ("import sys\nsys.path[:0] = ['x']\n", "sys.path"),
        ("import sys\nsys.path += ['x']\n", "sys.path"),
        ("import sys\nsys.path = ['x']\n", "sys.path"),
        ("import sys as s\ns.path.extend(['x'])\n", "sys.path"),
        ("from sys import path\npath.insert(0, 'x')\n", "sys.path"),
        ("import site\nsite.addsitedir('x')\n", "site.addsitedir"),
        ("from site import addsitedir as add\nadd('x')\n", "site.addsitedir"),
        (
            "import importlib.util\nimportlib.util.spec_from_file_location('m', 'x.py')\n",
            "importlib.util.spec_from_file_location",
        ),
        (
            "from importlib.util import spec_from_file_location\n"
            "spec_from_file_location('m', 'x.py')\n",
            "importlib.util.spec_from_file_location",
        ),
        (
            "import importlib.machinery\nimportlib.machinery.SourceFileLoader('m', 'x.py')\n",
            "importlib.machinery.SourceFileLoader",
        ),
        ("import runpy\nrunpy.run_path('x.py')\n", "runpy.run_path"),
    ],
)
def test_each_path_import_form_is_a_site(source: str, target: str) -> None:
    assert _sites(source) == {f"{_SKILL}::{target}": [2]}


@pytest.mark.parametrize(
    "source",
    [
        "import sys\nprint(sys.path)\n",
        "import sys\nfound = 'x' in sys.path\n",
        "items = []\nitems.insert(0, 'x')\n",
        "import os\npath = os.path\npath.join('a', 'b')\n",
        "code = \"runpy.run_path('x.py')\"\n",
    ],
)
def test_reading_sys_path_and_unrelated_calls_are_not_sites(source: str) -> None:
    assert _sites(source) == {}


def test_only_ava_builtins_is_in_scope() -> None:
    source = "import sys\nsys.path.insert(0, 'x')\n"
    assert _sites(source, "cli/python_install.py") == {}
    assert _sites(source, "ava/shell/coding_tools/claude.py") == {}


# --- the narrow within-skill `__file__` guard exception --------------------


@pytest.mark.parametrize(
    ("source", "rel_path"),
    [
        # Own directory, pathlib style (lint_no_script_sibling_imports.py's
        # str(Path(__file__).resolve().parent) equivalent).
        (
            "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parent))\n",
            "ava_builtins/skills/gmail/scripts/feed.py",
        ),
        # Own directory, os.path style (the exact pattern
        # lint_no_script_sibling_imports.py documents).
        (
            "import sys, os\nsys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n",
            "ava_builtins/skills/ava-self-evolution/scripts/daily_scan.py",
        ),
        # .append(...) is equally recognized, not just .insert(0, ...).
        (
            "import sys\nfrom pathlib import Path\n"
            "sys.path.append(str(Path(__file__).resolve().parent))\n",
            "ava_builtins/skills/gmail/scripts/feed.py",
        ),
        # A sibling sub-skill's scripts/ dir, pathlib style with a `/` tail —
        # still inside the same top-level skill (web-ai).
        (
            "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / 'scripts'))\n",
            "ava_builtins/skills/web-ai/console/scripts/ask.py",
        ),
        # Same, os.path.join style.
        (
            "import sys, os\n"
            "sys.path.insert(0, os.path.join("
            "os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))\n",
            "ava_builtins/skills/web-ai/console/scripts/ask.py",
        ),
        # The skill's own root directory (two hops up from scripts/) is still
        # inside the skill.
        (
            "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n",
            "ava_builtins/skills/gmail/scripts/feed.py",
        ),
    ],
)
def test_the_within_skill_file_guard_is_not_a_site(source: str, rel_path: str) -> None:
    assert _sites(source, rel_path) == {}


@pytest.mark.parametrize(
    ("source", "rel_path"),
    [
        # Reaches a DIFFERENT top-level skill entirely.
        (
            "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent "
            "/ 'audio-transcribe' / 'scripts'))\n",
            "ava_builtins/skills/web-sources/youtube/scripts/feed.py",
        ),
        # Escapes to ava_builtins/skills/ itself — not any one skill.
        (
            "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))\n",
            "ava_builtins/skills/gmail/scripts/feed.py",
        ),
        # A __file__-derived guard is never in scope outside skills/ at all.
        (
            "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parent))\n",
            "ava_builtins/plugins/ava_code/foo.py",
        ),
        # extend() is not a recognized mutator, even with a file-derived,
        # in-skill argument.
        (
            "import sys\nfrom pathlib import Path\n"
            "sys.path.extend([str(Path(__file__).resolve().parent)])\n",
            "ava_builtins/skills/gmail/scripts/feed.py",
        ),
        # insert() at a non-zero index is not the recognized guard shape.
        (
            "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(1, str(Path(__file__).resolve().parent))\n",
            "ava_builtins/skills/gmail/scripts/feed.py",
        ),
    ],
)
def test_a_guard_outside_its_own_skill_is_still_a_site(source: str, rel_path: str) -> None:
    assert _sites(source, rel_path) != {}


def test_a_file_loader_is_still_a_site_even_with_an_in_skill_file_derived_argument() -> None:
    source = (
        "import importlib.util\nfrom pathlib import Path\n"
        "importlib.util.spec_from_file_location("
        "'m', str(Path(__file__).resolve().parent / 'x.py'))\n"
    )
    assert _sites(source, "ava_builtins/skills/gmail/scripts/feed.py") == {
        "ava_builtins/skills/gmail/scripts/feed.py::importlib.util.spec_from_file_location": [3]
    }


# --- the gate ------------------------------------------------------------------
