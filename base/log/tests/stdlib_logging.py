"""Stdlib logging state that must not outlive a test: handlers and logger levels."""

import contextlib
from collections.abc import Generator

__all__ = ["stdlib_logging_isolated"]


@contextlib.contextmanager
def stdlib_logging_isolated() -> Generator[None]:
    """Run a block with the loguru->telemetry bridge off the root logger, then put stdlib
    logging back as it was: the root handlers AND the level of every logger the intercept
    sets.

    `install_stdlib_intercept()` sets the root to INFO and each first-party logger to
    DEBUG (plus the psycopg pool and uvicorn loggers). Levels live on the process-global
    `logging.Logger.manager`, so restoring only the handlers left them set for every later
    test in the same xdist worker. A stdlib call whose format string cannot consume its
    arguments raises out of pytest's capture handler only when the record passes the level
    gate, so the leak made such a bug fail only in the runs where an intercept test had
    come earlier in the worker.
    """
    import logging

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_levels = {
        name: logger.level
        for name, logger in logging.Logger.manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }
    root_level = root.level
    root.handlers = [
        handler
        for handler in saved_handlers
        if not (
            type(handler).__module__ == "base.log.sinks"
            and type(handler).__name__ == "StdlibInterceptHandler"
        )
    ]
    try:
        yield
    finally:
        root.handlers = saved_handlers
        root.setLevel(root_level)
        for name, logger in logging.Logger.manager.loggerDict.items():
            if isinstance(logger, logging.Logger):
                logger.setLevel(saved_levels.get(name, logging.NOTSET))
