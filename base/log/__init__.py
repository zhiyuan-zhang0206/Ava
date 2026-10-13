"""The shared lightweight logger and local sink mechanics.

Importing this module preserves caller logging without constructing process
resources or importing telemetry. Executable roots use
``base.log.process`` for process-level sink startup and
``base.telemetry.process_logging`` for the typed event-pipeline binding.
"""

from loguru import logger as logger

from base.log.sinks import (
    FILE_SINK_SIZE_LIMIT as FILE_SINK_SIZE_LIMIT,
)
from base.log.sinks import (
    FIRST_PARTY_LOGGER_NAMES as FIRST_PARTY_LOGGER_NAMES,
)
from base.log.sinks import (
    HUMAN_FORMAT as HUMAN_FORMAT,
)
from base.log.sinks import (
    GenaiEmptyPartsWarningFilter as GenaiEmptyPartsWarningFilter,
)
from base.log.sinks import (
    _install_stdlib_intercept as _install_stdlib_intercept,
)
from base.log.sinks import (
    _StdlibInterceptHandler as _StdlibInterceptHandler,
)
from base.log.sinks import (
    add_file_sink as add_file_sink,
)
from base.log.sinks import (
    add_sink as add_sink,
)
from base.log.sinks import (
    rotate_by_size_or_day as rotate_by_size_or_day,
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
