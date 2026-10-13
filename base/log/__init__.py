"""The shared lightweight logger and local sink mechanics.

Importing this module preserves caller logging without constructing process
resources or importing telemetry. Executable roots use
``base.log.process`` for process-level sink startup and
``base.telemetry.process_logging`` for the typed event-pipeline binding.
"""

from loguru import logger as logger

from base.log.sinks import (
    _FILE_SINK_SIZE_LIMIT as _FILE_SINK_SIZE_LIMIT,
)
from base.log.sinks import (
    _FIRST_PARTY_LOGGER_NAMES as _FIRST_PARTY_LOGGER_NAMES,
)
from base.log.sinks import (
    _HUMAN_FORMAT as _HUMAN_FORMAT,
)
from base.log.sinks import (
    _add_file_sink as _add_file_sink,
)
from base.log.sinks import (
    _GenaiEmptyPartsWarningFilter as _GenaiEmptyPartsWarningFilter,
)
from base.log.sinks import (
    _install_stdlib_intercept as _install_stdlib_intercept,
)
from base.log.sinks import (
    _rotate_by_size_or_day as _rotate_by_size_or_day,
)
from base.log.sinks import (
    _StdlibInterceptHandler as _StdlibInterceptHandler,
)
from base.log.sinks import (
    add_sink as add_sink,
)

__all__ = ["prepare_process_logging"]


def prepare_process_logging() -> None:
    """Prepare logging once at a fresh executable boundary, before configuration.

    This deliberately removes caller sinks. Embedded callers retain their
    logging and invoke dispatch or concrete initialization without this prelude.
    Importing this module never changes logging or constructs process resources.
    """
    logger.remove()
    logger.configure(extra={"agent_id": "-"})
