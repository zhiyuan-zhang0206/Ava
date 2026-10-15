import contextlib
from collections.abc import Generator

__all__ = ["stdlib_logging_isolated"]


@contextlib.contextmanager
def stdlib_logging_isolated() -> Generator[None]:
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
