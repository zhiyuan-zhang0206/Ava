"""Regression test: no logger sink renders local variables (secrets) into its output.

loguru's `diagnose` defaults to True. With it on, `logger.exception(...)` inlines
every local variable of the failing traceback's frames into the sink's rendered
output — an independent review reproduced this concretely by recovering a
`psycopg.connect` DSN's password from a `logger.exception`-logged connection
failure. Every sink in `base/log/__init__.py` and `base/log/sinks.py` goes through
`base.log.sinks.add_sink`, which passes `diagnose=False` (see
scripts/lint/diagnostics/logger_add_diagnose.py, which guards this repo-wide).

These tests exercise the actual production sink constructor —
`base.log.sinks._add_file_sink`, the file sink every `init_*` entry point in
`base/log/__init__.py` installs identically — rather than reimplementing sink
construction, so a regression in the real code path is what fails this test.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from loguru import logger

from base.log.sinks import _add_file_sink, add_sink

# Not a real credential — a fixture value chosen to be unmistakable in a diff/log.
_SENTINEL = "SENTINEL-PASSWORD"


@pytest.fixture
def _file_sink(tmp_path: Path) -> Iterator[Path]:
    """Register the production file sink alone, yield its path, remove it after.

    Only the sink this test adds is removed (`logger.remove(sink_id)`, never
    the bare no-arg form) — other handlers already registered on the process
    singleton (pytest's own, other fixtures') are left alone.
    """
    log_path = tmp_path / "test.log"
    sink_id = _add_file_sink(log_path)
    try:
        yield log_path
    finally:
        logger.remove(sink_id)


def _fail_while_holding(_password: str) -> None:
    raise RuntimeError("boom")


def _raise_with_secret_on_the_frame() -> None:
    # diagnose renders the value of any name referenced on a traceback source
    # line (this is exactly the mechanism the psycopg/DSN case below exploits:
    # `dsn` is a plain argument on the failing call's line) — so the secret
    # must appear on the call line itself, not merely exist as an unused local.
    db_password = _SENTINEL
    _fail_while_holding(db_password)


def test_exception_local_variable_is_not_rendered_into_the_sink(_file_sink: Path) -> None:
    try:
        _raise_with_secret_on_the_frame()
    except RuntimeError:
        logger.exception("failure in _raise_with_secret_on_the_frame")
    content = _file_sink.read_text(encoding="utf-8")
    assert "boom" in content, "sanity check: the log line must actually have been written"
    assert _SENTINEL not in content


def test_psycopg_connect_dsn_password_is_not_rendered_into_the_sink(_file_sink: Path) -> None:
    """A DSN's password lives as a local in psycopg's connect frames when the
    connection attempt fails — the concrete leak the independent review found."""
    dsn = f"postgresql://u:{_SENTINEL}@127.0.0.1:1/x"
    try:
        psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError:
        logger.exception("failed to connect")
    else:
        pytest.fail("connect to 127.0.0.1:1 unexpectedly succeeded")
    content = _file_sink.read_text(encoding="utf-8")
    assert _SENTINEL not in content


def test_add_sink_refuses_an_explicit_diagnose_request() -> None:
    """A caller asking for `diagnose=True` fails loud and registers no sink."""
    received: list[str] = []
    with pytest.raises(ValueError, match="diagnose=True is forbidden"):
        add_sink(received.append, diagnose=True)
    logger.info("must reach no refused sink")
    assert received == []
