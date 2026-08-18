from __future__ import annotations

import importlib

_lint = importlib.import_module("scripts.lint_fail_fast")


def _violations(src: str) -> list[tuple[str, int, str]]:
    return _lint.violations_in_source(src, "fixture.py")


def test_bare_except_pass_is_flagged():
    src = "try:\n    risky()\nexcept:\n    pass\n"
    assert len(_violations(src)) == 1


def test_typed_except_pass_is_flagged():
    src = "try:\n    risky()\nexcept ValueError:\n    pass\n"
    assert len(_violations(src)) == 1


def test_multiline_except_pass_is_flagged():
    # The `except` and `pass` on separate lines — a grep for `except.*: pass`
    # would miss this; the AST walk catches it.
    src = "try:\n    risky()\nexcept ValueError:\n\n    pass\n"
    assert len(_violations(src)) == 1


def test_contextlib_suppress_is_clean():
    src = "import contextlib\nwith contextlib.suppress(ValueError):\n    risky()\n"
    assert _violations(src) == []


def test_handler_that_logs_and_reraises_is_clean():
    src = "try:\n    risky()\nexcept ValueError:\n    log()\n    raise\n"
    assert _violations(src) == []


def test_fail_fast_ok_on_except_line_is_exempt():
    src = "try:\n    risky()\nexcept ValueError:  # fail-fast-ok: reason\n    pass\n"
    assert _violations(src) == []


def test_fail_fast_ok_on_pass_line_is_exempt():
    src = "try:\n    risky()\nexcept ValueError:\n    pass  # fail-fast-ok: reason\n"
    assert _violations(src) == []


def test_real_tree_has_zero_violations():
    # After the burn-down, the live framework tree must be clean.
    assert _lint.main() == 0
