"""`scripts/lint/loguru_format.py` — no loguru call that drops its arguments.

loguru formats with `str.format`, so a stdlib-style `%s` message logs the
literal placeholder and loses every argument. These cases pin what the lint
flags (printf placeholders on each way a module reaches the loguru logger,
arguments with no `{}` field) and what it must leave alone: `{}` messages, a
stdlib `logging` logger (for which `%s` is correct), ambiguous names, and the
inline exemption.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

_lint = importlib.import_module("scripts.lint.loguru_format")


def _lines(src: str) -> list[int]:
    return [lineno for lineno, _ in _lint.violations_in_source(src)]


def test_printf_placeholder_on_shared_log_logger_is_flagged() -> None:
    src = 'from shared.log import logger\nlogger.warning("gate for %s raised: %s", name, exc)\n'
    violations = _lint.violations_in_source(src)
    assert [lineno for lineno, _ in violations] == [2]
    assert "'%s'" in violations[0][1]


@pytest.mark.parametrize(
    "src",
    [
        'from loguru import logger as _log\n_log.info("n=%d", n)\n',
        'import loguru\nloguru.logger.error("x %r", x)\n',
        'from shared.log import logger\nlogger.opt(exception=e).warning("x %s", x)\n',
        'from shared.log import logger\nlog = logger.bind(c=1)\nlog.debug("x %s", x)\n',
        'from shared.log import logger\nlogger.log("INFO", "took %.1fs", t)\n',
        'from shared.log import logger\nlogger.info("x %(k)s", k=1)\n',
        'from shared.log import logger\nlogger.info(f"{a} then %s", b)\n',
        'def f():\n    from loguru import logger\n    logger.info("x %s", x)\n',
    ],
)
def test_every_route_to_loguru_is_checked(src: str) -> None:
    assert len(_lines(src)) == 1


def test_positional_args_without_any_field_are_flagged() -> None:
    src = 'from shared.log import logger\nlogger.info("done", count)\n'
    violations = _lint.violations_in_source(src)
    assert len(violations) == 1
    assert "no `{}` field" in violations[0][1]


@pytest.mark.parametrize(
    "src",
    [
        'from shared.log import logger\nlogger.warning("gate for {} raised: {}", name, exc)\n',
        'from shared.log import logger\nlogger.info("100% done")\n',
        'from shared.log import logger\nlogger.info("50%% of {}", total)\n',
        'from shared.log import logger\nlogger.info("rate %s")\n',
        "from shared.log import logger\nlogger.info(message, x)\n",
        'from shared.log import logger\nlogger.info("{:.0f}s", t)\n',
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
        "from shared.log import logger\nimport logging\n"
        'def f():\n    logger = logging.getLogger("x")\n    logger.info("x %s", x)\n',
        'from shared.log import logger\ndef f(logger):\n    logger.info("x %s", x)\n',
    ],
)
def test_stdlib_logging_is_never_flagged(src: str) -> None:
    assert _lines(src) == []


def test_exemption_marker_on_any_line_of_the_call() -> None:
    src = (
        "from shared.log import logger\n"
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
        'from shared.log import logger\nlogger.info("x %s", x)\n', encoding="utf-8"
    )
    assert _lint.main([str(tmp_path)]) == 1
