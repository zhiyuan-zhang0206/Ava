"""`scripts/lint/loguru_format.py` — every log call's message format matches its logger.

loguru formats with `str.format`, so a stdlib-style `%s` message logs the
literal placeholder and loses every argument. These cases pin what the lint
flags (printf placeholders on each way a module reaches the loguru logger,
arguments with no `{}` field) and what it must leave alone: `{}` messages, a
stdlib `logging` logger (for which `%s` is correct), ambiguous names, and the
inline exemption.

The mirror rule: stdlib logging formats with `%`, so a loguru-style `{}` message
with positional arguments raises `TypeError` while the record is formatted. The
two loggers are told apart by where each name comes from, never by the file, so
the mixed-module cases below are the ones that matter.

Rule 3: loguru has no `exc_info` parameter — the kwarg is flagged on loguru log
calls (the traceback would be lost) and left alone on stdlib ones (task #4979).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

_lint = importlib.import_module("scripts.lint.loguru_format")


def _lines(src: str) -> list[int]:
    return [lineno for lineno, _ in _lint.violations_in_source(src)]


def test_printf_placeholder_on_base_log_logger_is_flagged() -> None:
    src = 'from base.log import logger\nlogger.warning("gate for %s raised: %s", name, exc)\n'
    violations = _lint.violations_in_source(src)
    assert [lineno for lineno, _ in violations] == [2]
    assert "'%s'" in violations[0][1]


@pytest.mark.parametrize(
    "src",
    [
        'from loguru import logger as _log\n_log.info("n=%d", n)\n',
        'import loguru\nloguru.logger.error("x %r", x)\n',
        'from base.log import logger\nlogger.opt(exception=e).warning("x %s", x)\n',
        'from base.log import logger\nlog = logger.bind(c=1)\nlog.debug("x %s", x)\n',
        'from base.log import logger\nlogger.log("INFO", "took %.1fs", t)\n',
        'from base.log import logger\nlogger.info("x %(k)s", k=1)\n',
        'from base.log import logger\nlogger.info(f"{a} then %s", b)\n',
        'def f():\n    from loguru import logger\n    logger.info("x %s", x)\n',
    ],
)
def test_every_route_to_loguru_is_checked(src: str) -> None:
    assert len(_lines(src)) == 1


def test_positional_args_without_any_field_are_flagged() -> None:
    src = 'from base.log import logger\nlogger.info("done", count)\n'
    violations = _lint.violations_in_source(src)
    assert len(violations) == 1
    assert "no `{}` field" in violations[0][1]


@pytest.mark.parametrize(
    "src",
    [
        'from base.log import logger\nlogger.warning("gate for {} raised: {}", name, exc)\n',
        'from base.log import logger\nlogger.info("100% done")\n',
        'from base.log import logger\nlogger.info("50%% of {}", total)\n',
        'from base.log import logger\nlogger.info("rate %s")\n',
        "from base.log import logger\nlogger.info(message, x)\n",
        'from base.log import logger\nlogger.info("{:.0f}s", t)\n',
    ],
)
def test_correct_or_unverifiable_calls_are_clean(src: str) -> None:
    assert _lines(src) == []


@pytest.mark.parametrize(
    "src",
    [
        'import logging\nlogger = logging.getLogger(__name__)\nlogger.info("x %s", x)\n',
        'import logging\n_log = logging.getLogger("svc")\n_log.warning("x %s", x)\n',
        'import logging\ndef f(log: logging.Logger):\n    log.info("x %s", x)\n',
        # The same name bound both ways is ambiguous — never flagged.
        "from base.log import logger\nimport logging\n"
        'def f():\n    logger = logging.getLogger("x")\n    logger.info("x %s", x)\n',
        'from base.log import logger\ndef f(logger):\n    logger.info("x %s", x)\n',
    ],
)
def test_stdlib_logging_with_printf_placeholders_is_clean(src: str) -> None:
    assert _lines(src) == []


def test_exemption_marker_on_any_line_of_the_call() -> None:
    src = (
        "from base.log import logger\n"
        "logger.info(\n"
        '    "literal %s",  # log-format-ok: proves the drop\n'
        "    x,\n"
        ")\n"
    )
    assert _lines(src) == []


def test_real_tree_has_zero_violations() -> None:
    assert _lint.main([]) == 0


def test_explicit_missing_target_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "ok.py"
    good.write_text("value = 1\n", encoding="utf-8")
    missing = tmp_path / "typo.py"
    assert _lint.main([str(missing)]) == 1
    assert str(missing) in capsys.readouterr().err
    assert _lint.main([str(good), str(missing)]) == 1


def test_explicit_directory_target_is_scanned(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("value = 1\n", encoding="utf-8")
    assert _lint.main([str(tmp_path)]) == 0
    (tmp_path / "bad.py").write_text(
        'from base.log import logger\nlogger.info("x %s", x)\n', encoding="utf-8"
    )
    assert _lint.main([str(tmp_path)]) == 1


# ── Rule 2: stdlib loggers take `%`, not `{}` ─────────────────────────────────


@pytest.mark.parametrize(
    "src",
    [
        'import logging\n_log = logging.getLogger("svc")\n_log.info("n={}", n)\n',
        'import logging\nlogger = logging.getLogger(__name__)\nlogger.error("x {name}", n)\n',
        'import logging\n_log = logging.getLogger("svc")\n_log.warning("x {0} {1}", a, b)\n',
        'import logging\n_log = logging.getLogger("svc")\n_log.info("t={:.1f}s", t)\n',
        'import logging\n_log = logging.getLogger("svc")\n_log.exception("keeping {}s", t)\n',
        'import logging\n_log = logging.getLogger("svc")\n_log.info(f"{a} then {{}}", b)\n',
        'import logging as lg\n_log = lg.getLogger("svc")\n_log.info("n={}", n)\n',
        'from logging import getLogger\n_log = getLogger("svc")\n_log.info("n={}", n)\n',
        'from logging import getLogger as make\n_log = make("svc")\n_log.info("n={}", n)\n',
        'import logging.handlers\n_log = logging.getLogger("svc")\n_log.info("n={}", n)\n',
        # No module-level name at all: the call expression itself, a child, an alias,
        # the `.log(level, ...)` form, the deprecated `warn`, the root-logger shortcut.
        'import logging\nlogging.getLogger(__name__).warning("n={}", n)\n',
        'import logging\n_log = logging.getLogger("a")\nchild = _log.getChild("b")\nchild.info("n={}", n)\n',
        'import logging\n_log = logging.getLogger("a")\nalias = _log\nalias.info("n={}", n)\n',
        'import logging\n_log = logging.getLogger("a")\n_log.log(logging.INFO, "n={}", n)\n',
        'import logging\n_log = logging.getLogger("a")\n_log.warn("n={}", n)\n',
        'import logging\nlogging.warning("n={}", n)\n',
        'import logging\n_log = logging.getLogger("a")\n_log.info("n={}", *rest)\n',
        # A keyword such as exc_info= is not a format argument; the positional one is.
        'import logging\n_log = logging.getLogger("a")\n_log.info("n={}", n, exc_info=True)\n',
        "import logging\ndef f():\n    _log = logging.getLogger('a')\n    _log.info('n={}', n)\n",
    ],
)
def test_loguru_style_fields_on_a_stdlib_logger_are_flagged(src: str) -> None:
    violations = _lint.violations_in_source(src)
    assert len(violations) == 1
    assert "stdlib logging call" in violations[0][1]
    assert "%s" in violations[0][1]


def test_the_stdlib_message_names_the_field_and_the_line() -> None:
    src = (
        "import logging\n"
        '_log = logging.getLogger("svc")\n'
        "_log.info(\n"
        '    "flush pass: delivered={} expired={}",\n'
        "    delivered,\n"
        "    expired,\n"
        ")\n"
    )
    violations = _lint.violations_in_source(src)
    assert [lineno for lineno, _ in violations] == [3]
    assert "'{}'" in violations[0][1]


@pytest.mark.parametrize(
    "src",
    [
        'import logging\n_log = logging.getLogger("a")\n_log.info("n=%s", n)\n',
        'import logging\n_log = logging.getLogger("a")\n_log.info("n=%d rate=%.1f", n, r)\n',
        # No positional argument: the braces are just text.
        'import logging\n_log = logging.getLogger("a")\n_log.info("n={}")\n',
        'import logging\n_log = logging.getLogger("a")\n_log.info("payload {}", extra={"k": 1})\n',
        'import logging\n_log = logging.getLogger("a")\n_log.info("json {\\"k\\": 1}")\n',
        # A printf conversion consumes the argument; the braces are literal text.
        'import logging\n_log = logging.getLogger("a")\n_log.info("{} then %s", n)\n',
        'import logging\n_log = logging.getLogger("a")\n_log.info("{name}: %(k)s", {"k": 1})\n',
        # `{{` / `}}` are escapes, not fields.
        'import logging\n_log = logging.getLogger("a")\n_log.info("use {{}} for %s", n)\n',
        # Not a literal message, or not a stdlib logger.
        'import logging\n_log = logging.getLogger("a")\n_log.info(template, n)\n',
        'import logging\n_log = logging.getLogger("a")\n_log.setLevel("{}", n)\n',
        'import logging\nlogging.basicConfig(format="{message}", style="{")\n',
        'import logging\n_log = logging.getLogger("a")\n_log.info("x {}".format(n))\n',
        'import logging\ndef f(log: logging.Logger):\n    log.info("n={}", n)\n',
        'import logging\nother = make()\nother.info("n={}", n)\n',
        'import logging\nlogging.info("n=%s", n)\n',
    ],
)
def test_correct_or_unverifiable_stdlib_calls_are_clean(src: str) -> None:
    assert _lines(src) == []


def test_a_loguru_logger_keeps_its_braces_in_a_module_that_imports_logging() -> None:
    src = (
        "import logging\n"
        "from base.log import logger\n"
        'logger.info("n={}", n)\n'
        'logger.warning("a {} b {}", a, b)\n'
    )
    assert _lines(src) == []


def test_a_module_with_both_loggers_is_judged_per_variable() -> None:
    src = (
        "import logging\n"
        "from base.log import logger\n"
        '_log = logging.getLogger("svc")\n'
        'logger.info("a={}", a)\n'  # 4: loguru, braces: fine
        '_log.info("a=%s", a)\n'  # 5: stdlib, printf: fine
        '_log.info("a={}", a)\n'  # 6: stdlib, braces: flagged
        'logger.info("a=%s", a)\n'  # 7: loguru, printf: flagged
    )
    violations = _lint.violations_in_source(src)
    assert [lineno for lineno, _ in violations] == [6, 7]
    assert "stdlib logging call" in violations[0][1]
    assert "loguru call" in violations[1][1]


def test_a_module_with_both_loggers_accepts_each_in_its_own_style() -> None:
    src = (
        "import logging\n"
        "from base.log import logger\n"
        '_log = logging.getLogger("svc")\n'
        'logger.info("a={}", a)\n'
        '_log.info("a=%s", a)\n'
    )
    assert _lines(src) == []


def test_a_name_bound_as_both_stdlib_and_loguru_is_ambiguous_for_both_rules() -> None:
    src = (
        "import logging\n"
        "from base.log import logger as _log\n"
        "def f():\n"
        '    _log = logging.getLogger("x")\n'
        '    _log.info("a={}", a)\n'
        '    _log.info("a=%s", a)\n'
    )
    assert _lines(src) == []


@pytest.mark.parametrize(
    "src",
    [
        # A rebinding, a parameter or a loop target makes the stdlib name unverifiable.
        'import logging\n_log = logging.getLogger("a")\n_log = make()\n_log.info("n={}", n)\n',
        'import logging\n_log = logging.getLogger("a")\ndef f(_log):\n    _log.info("n={}", n)\n',
        'import logging\n_log = logging.getLogger("a")\nfor _log in many:\n    _log.info("n={}", n)\n',
    ],
)
def test_a_stdlib_name_rebound_another_way_is_not_checked(src: str) -> None:
    assert _lines(src) == []


def test_exemption_marker_covers_a_stdlib_call() -> None:
    src = (
        "import logging\n"
        '_log = logging.getLogger("svc")\n'
        "_log.info(\n"
        '    "literal {}",  # log-format-ok: proves the drop\n'
        "    x,\n"
        ")\n"
    )
    assert _lines(src) == []


def test_a_stdlib_violation_fails_the_cli_and_names_both_formatters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        'import logging\n_log = logging.getLogger("svc")\n_log.info("n={}", n)\n',
        encoding="utf-8",
    )
    assert _lint.main([str(bad)]) == 1
    captured = capsys.readouterr()
    assert f"{bad}:3:" in captured.out
    assert "`{}`" in captured.err and "`%s`" in captured.err


# ── Rule 3: loguru has no `exc_info` ──────────────────────────────────────────


@pytest.mark.parametrize(
    "src",
    [
        'from base.log import logger\nlogger.warning("gate raised", exc_info=True)\n',
        'from base.log import logger\nexc = RuntimeError("x")\nlogger.error("failed", exc_info=exc)\n',
        'from base.log import logger\nlogger.exception("failed", exc_info=True)\n',
        'from base.log import logger\nlogger.opt(depth=1).warning("failed", exc_info=True)\n',
        'from base.log import logger\nlog = logger.bind(c=1)\nlog.debug("failed", exc_info=True)\n',
        'import loguru\nloguru.logger.critical("failed", exc_info=True)\n',
        'from base.log import logger\nlogger.log("INFO", "failed", exc_info=True)\n',
    ],
)
def test_exc_info_on_a_loguru_call_is_flagged(src: str) -> None:
    violations = _lint.violations_in_source(src)
    assert len(violations) == 1
    assert "exc_info" in violations[0][1]
    assert "opt(exception=True)" in violations[0][1]


@pytest.mark.parametrize(
    "src",
    [
        # stdlib logging accepts exc_info — the same kwarg is correct there.
        'import logging\n_log = logging.getLogger("svc")\n_log.warning("failed", exc_info=True)\n',
        # loguru's own forms carry the exception; calls without the kwarg are clean.
        'from base.log import logger\nlogger.opt(exception=True).warning("failed")\n',
        'from base.log import logger\nlogger.opt(exception=exc).warning("failed")\n',
        'from base.log import logger\nlogger.warning("failed", event="x", agent_id=1)\n',
        # A name bound both ways is ambiguous — never flagged.
        "from base.log import logger\nimport logging\ndef f():\n"
        '    logger = logging.getLogger("x")\n    logger.warning("failed", exc_info=True)\n',
    ],
)
def test_exc_info_outside_loguru_log_calls_is_clean(src: str) -> None:
    assert _lines(src) == []


def test_exemption_marker_covers_the_exc_info_rule() -> None:
    src = (
        "from base.log import logger\n"
        'logger.warning("failed", exc_info=True)  # log-format-ok: proves it\n'
    )
    assert _lines(src) == []
