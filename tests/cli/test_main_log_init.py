"""Regression coverage for fail-soft detached CLI logging assembly."""

from __future__ import annotations

import importlib

import pytest


def test_cli_import_survives_logging_sink_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recovery CLI child must import while its Postgres sink is unavailable."""
    import cli.main as cli_main
    from shared import log

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setenv("AVA_CLI_LOG_NAME", "updater")
    monkeypatch.setattr(log, "init_cli_process", _raise)
    importlib.reload(cli_main)
