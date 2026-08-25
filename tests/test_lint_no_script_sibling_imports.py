"""`scripts/lint_no_script_sibling_imports.py` — the PYTHONSAFEPATH sibling-import guard.

A script-mode file (main block or python/uv shebang) may not import a
same-directory sibling unless a `sys.path.insert` / `sys.path.append` call
runs before it — module top (including top-level containers) or earlier in
the same function. See the script header for the full rule and scope.
"""

from __future__ import annotations

import importlib
import textwrap
from pathlib import Path

import pytest

_lint = importlib.import_module("scripts.lint_no_script_sibling_imports")


@pytest.fixture()
def scan_tmp(tmp_path: Path, monkeypatch) -> None:
    """Point the lint at a scratch tree so fixtures never touch the real repo."""
    monkeypatch.setattr(_lint, "_REPO_ROOT", tmp_path)


def _write(scan_tmp, rel: str, body: str) -> Path:
    p = _lint._REPO_ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def _errors(scan_tmp, rel: str, body: str) -> list[str]:
    return _lint._scan_file(_write(scan_tmp, rel, body))


# All bodies are assembled at one uniform indentation so dedent produces a
# valid module (mixed template indentation would leave stray leading spaces
# and parse as IndentationError).
def _script(imports: str, guard: str = "") -> str:
    return textwrap.dedent(
        f"""\
        {guard}{imports}
        import sys

        def main() -> int:
            return 0

        if __name__ == "__main__":
            sys.exit(main())
        """
    )


def test_unguarded_sibling_import_in_script_is_rejected(scan_tmp) -> None:
    _write(scan_tmp, "tools/helper.py", "VALUE = 1\n")
    errs = _errors(scan_tmp, "tools/runner.py", _script("from helper import VALUE"))
    assert len(errs) == 1
    assert "tools/runner.py:1" in errs[0]
    assert "helper" in errs[0]
    assert "sys.path.insert" in errs[0]


def test_guard_before_import_is_accepted(scan_tmp) -> None:
    _write(scan_tmp, "tools/helper.py", "VALUE = 1\n")
    guard = (
        "import os\n"
        "import sys\n"
        "\n"
        "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
        "\n"
    )
    errs = _errors(scan_tmp, "tools/runner.py", _script("from helper import VALUE", guard))
    assert errs == []


def test_guard_after_import_is_rejected(scan_tmp) -> None:
    _write(scan_tmp, "tools/helper.py", "VALUE = 1\n")
    body = textwrap.dedent(
        """\
        import os
        import sys

        from helper import VALUE

        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

        def main() -> int:
            return 0

        if __name__ == "__main__":
            sys.exit(main())
        """
    )
    errs = _errors(scan_tmp, "tools/runner.py", body)
    assert len(errs) == 1


def test_guard_inside_top_level_if_is_accepted(scan_tmp) -> None:
    """feed.py pattern: `if str(_HERE) not in sys.path: sys.path.insert(...)`."""
    _write(scan_tmp, "mail/_imap.py", "VALUE = 1\n")
    body = textwrap.dedent(
        """\
        import sys
        from pathlib import Path

        _HERE = str(Path(__file__).resolve().parent)
        if _HERE not in sys.path:
            sys.path.insert(0, _HERE)

        from _imap import VALUE

        def main() -> int:
            return 0

        if __name__ == "__main__":
            sys.exit(main())
        """
    )
    errs = _errors(scan_tmp, "mail/feed.py", body)
    assert errs == []


def test_in_function_guard_before_in_function_import_is_accepted(scan_tmp) -> None:
    """aggregate.py pattern: the guard immediately precedes the import, in-function."""
    _write(scan_tmp, "tools/helper.py", "VALUE = 1\n")
    body = textwrap.dedent(
        """\
        import sys
        from pathlib import Path

        def load() -> int:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from helper import VALUE

            return VALUE

        if __name__ == "__main__":
            print(load())
        """
    )
    errs = _errors(scan_tmp, "tools/runner.py", body)
    assert errs == []


def test_module_only_file_is_not_flagged(scan_tmp) -> None:
    _write(scan_tmp, "mail/_imap.py", "VALUE = 1\n")
    errs = _errors(
        scan_tmp,
        "mail/_smtp.py",
        """
        from _imap import VALUE

        def send() -> int:
            return VALUE
        """,
    )
    assert errs == []


def test_shebang_script_without_guard_is_rejected(scan_tmp) -> None:
    _write(scan_tmp, "tools/helper.py", "VALUE = 1\n")
    body = textwrap.dedent(
        """\
        #!/usr/bin/env python3
        from helper import VALUE

        print(VALUE)
        """
    )
    errs = _errors(scan_tmp, "tools/probe.py", body)
    assert len(errs) == 1
    assert "tools/probe.py:2" in errs[0]


def test_uv_script_shebang_is_script_mode(scan_tmp) -> None:
    _write(scan_tmp, "tools/helper.py", "VALUE = 1\n")
    body = textwrap.dedent(
        """\
        #!/usr/bin/env -S uv run --script
        from helper import VALUE

        print(VALUE)
        """
    )
    errs = _errors(scan_tmp, "tools/probe.py", body)
    assert len(errs) == 1


def test_dotted_from_sibling_file_is_not_flagged(scan_tmp) -> None:
    """`from ops.controllers.respawn import X` with a sibling ops.py FILE resolves
    to the top-level ops package, not the sibling — a file cannot be a package."""
    _write(scan_tmp, "health/ops.py", "VALUE = 1\n")
    errs = _errors(
        scan_tmp, "health/restarter.py", _script("from ops.controllers.respawn import VALUE")
    )
    assert errs == []


def test_bare_import_of_sibling_file_is_flagged_even_when_stdlib_shadows(scan_tmp) -> None:
    """`import platform` with a sibling platform.py resolves to the sibling in
    script mode — the conservative direction is to flag it."""
    _write(scan_tmp, "shared/platform.py", "VALUE = 1\n")
    errs = _errors(scan_tmp, "shared/macos_firewall.py", _script("import platform"))
    assert len(errs) == 1


def test_sibling_package_import_is_rejected(scan_tmp) -> None:
    _write(scan_tmp, "tools/pkg/__init__.py", "\n")
    _write(scan_tmp, "tools/pkg/mod.py", "VALUE = 1\n")
    errs = _errors(scan_tmp, "tools/runner.py", _script("from pkg.mod import VALUE"))
    assert len(errs) == 1
    assert "pkg" in errs[0]


def test_type_checking_sibling_import_is_not_flagged(scan_tmp) -> None:
    _write(scan_tmp, "tools/helper.py", "VALUE = 1\n")
    body = textwrap.dedent(
        """\
        from typing import TYPE_CHECKING

        if TYPE_CHECKING:
            from helper import VALUE

        import sys

        def main() -> int:
            return 0

        if __name__ == "__main__":
            sys.exit(main())
        """
    )
    errs = _errors(scan_tmp, "tools/runner.py", body)
    assert errs == []


def test_out_of_repo_path_does_not_crash(scan_tmp, tmp_path_factory) -> None:
    """A target outside _REPO_ROOT must not crash the run (bare relative_to used
    to raise ValueError; the lint reports it by absolute path instead)."""
    outside = tmp_path_factory.mktemp("outside")
    (outside / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    bad = outside / "runner.py"
    bad.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            from helper import VALUE

            print(VALUE)
            """
        ),
        encoding="utf-8",
    )
    assert _lint.main([str(bad)]) == 1


def test_main_returns_nonzero_on_violation(scan_tmp) -> None:
    _write(scan_tmp, "tools/helper.py", "VALUE = 1\n")
    bad = _write(scan_tmp, "tools/runner.py", _script("from helper import VALUE"))
    assert _lint.main([str(bad)]) == 1
    good = _write(
        scan_tmp,
        "tools/clean.py",
        """
        VALUE = 1

        def main() -> int:
            return 0
        """,
    )
    assert _lint.main([str(good)]) == 0
