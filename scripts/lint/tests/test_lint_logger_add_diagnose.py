"""`scripts/lint/diagnostics/logger_add_diagnose.py` — no logger.add(...) sink without diagnose=False.

The invariant this guards: loguru's `diagnose` defaults to True, so any sink
that does not turn it off has `logger.exception(...)` render every local
variable of the failing traceback's frames — including secrets like a DSN
password — into that sink's output. Before this lint, every `logger.add(...)`
call on `main` left `diagnose` unset. These cases pin the shape a real call
site takes (missing the kwarg entirely, a non-`False` literal, an unverifiable
name/kwargs-unpack) plus what must NOT be flagged: an explicit `diagnose=False`
and calls on an object that is not a logger.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

_lint = importlib.import_module("scripts.lint.diagnostics.logger_add_diagnose")


def _violations(src: str) -> list[tuple[int, str]]:
    return _lint.violations_in_source(src)


def test_add_with_no_diagnose_kwarg_is_flagged():
    src = 'logger.add(sys.stderr, format="{message}")\n'
    violations = _violations(src)
    assert len(violations) == 1
    assert "passes no `diagnose=False`" in violations[0][1]


def test_add_with_diagnose_false_is_clean():
    src = 'logger.add(sys.stderr, format="{message}", diagnose=False)\n'
    assert _violations(src) == []


def test_add_with_diagnose_true_is_flagged():
    src = "logger.add(sys.stderr, diagnose=True)\n"
    violations = _violations(src)
    assert len(violations) == 1
    assert "must pass the literal `False`" in violations[0][1]


def test_add_with_non_literal_diagnose_is_flagged():
    # The lint reads source; a name it cannot see through would be an
    # unverifiable pass, so it is treated as a miss rather than trusted.
    src = "logger.add(sys.stderr, diagnose=some_flag)\n"
    violations = _violations(src)
    assert len(violations) == 1
    assert "must pass the literal `False`" in violations[0][1]


def test_add_with_kwargs_unpack_only_is_flagged():
    # `**opts` carries no literal `diagnose=False` the lint can see statically
    # (its AST keyword has `arg=None`, so the `diagnose` lookup never matches
    # it) — fail-closed treats this the same as an omitted kwarg entirely,
    # even if the dict happens to hold `diagnose=False` at runtime.
    src = "logger.add(sink, **opts)\n"
    violations = _violations(src)
    assert len(violations) == 1
    assert "passes no `diagnose=False`" in violations[0][1]


def test_add_with_explicit_diagnose_false_and_kwargs_unpack_is_clean():
    # The literal keyword still wins when it rides alongside a `**opts`
    # unpack for the sink's other settings.
    src = "logger.add(sink, diagnose=False, **opts)\n"
    assert _violations(src) == []


def test_underscore_logger_alias_is_covered():
    src = "_logger.add(sink, diagnose=True)\n"
    assert len(_violations(src)) == 1


def test_attribute_logger_is_covered():
    # e.g. `self.logger.add(...)`
    src = "self.logger.add(sink)\n"
    violations = _violations(src)
    assert len(violations) == 1
    assert "passes no `diagnose=False`" in violations[0][1]


def test_logger_name_in_any_case_is_covered():
    # `_global_logger` is base/agents/tests/test_log_sink.py's real shape; a name
    # that breaks the lowercase convention is still a loguru sink.
    for receiver in ("_global_logger", "LOGGER", "runLogger", "self.Logger"):
        assert len(_violations(f"{receiver}.add(sink)\n")) == 1, receiver


def test_multiline_call_with_diagnose_false_is_clean():
    src = (
        'logger.add(\n    path,\n    serialize=True,\n    level="DEBUG",\n    diagnose=False,\n)\n'
    )
    assert _violations(src) == []


def test_unrelated_add_call_is_not_a_construction():
    # `.add(` on something that is not a logger-named object must not match.
    src = "queue.add(item)\n"
    assert _violations(src) == []


def test_repo_is_clean() -> None:
    """The gate is only as good as its verdict on the tree it guards: every
    logger.add(...) call under the scanned directories must already pass."""
    assert _lint.main([]) == 0


def test_explicit_missing_target_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd explicit path must fail the gate, not pass as a silent empty scan."""
    good = tmp_path / "ok.py"
    good.write_text("value = 1\n", encoding="utf-8")
    missing = tmp_path / "typo.py"
    assert _lint.main([str(missing)]) == 1
    assert str(missing) in capsys.readouterr().err
    assert _lint.main([str(good), str(missing)]) == 1


def test_directory_with_dangling_symlink_member_is_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken *.py symlink inside an explicit directory must be skipped like
    any unreadable entry — the scan must not crash on it, and a violating
    sibling file is still reported."""
    (tmp_path / "ok.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "dangling.py").symlink_to(tmp_path / "missing.py")
    assert _lint.main([str(tmp_path)]) == 0
    (tmp_path / "viol.py").write_text("logger.add(sys.stderr)\n", encoding="utf-8")
    assert _lint.main([str(tmp_path)]) == 1
    assert "passes no `diagnose=False`" in capsys.readouterr().out


def test_test_file_is_exempt(tmp_path: Path) -> None:
    """A test fixture that mounts a throwaway sink against a captured list has
    no secret in its local frames to leak — exempt like scripts/lint/pool_keepalives.py
    exempts tests/ from its equivalent rule."""
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    (test_dir / "test_something.py").write_text("logger.add(sys.stderr)\n", encoding="utf-8")
    assert _lint.main([str(test_dir)]) == 0
