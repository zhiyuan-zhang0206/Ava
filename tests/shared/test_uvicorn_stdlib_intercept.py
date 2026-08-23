"""`uvicorn` stdlib logging flows through `_StdlibInterceptHandler` (#970).

Bug: the gateway's `uvicorn.run(...)` used uvicorn's default `log_config`
(`LOGGING_CONFIG`), whose `dictConfig` clobbered the root-handler install
(`_StdlibInterceptHandler`) that `init_gateway_process` set up. uvicorn's own
records — most importantly the unhandled-ASGI-exception traceback
("Exception in ASGI application ...") — then went to a bare stderr handler:
only the pane, lost forever when the session was recreated. A memory
search 500's gateway-side traceback was unforensicable.

Fix: `uvicorn.run(..., log_config=None)` leaves the logging system alone, so
`uvicorn.error` propagates to the root intercept handler and reaches the
loguru sinks (gateway.log + events table). `_install_stdlib_intercept` gates
`uvicorn.access` to WARNING — per-request INFO would otherwise flood the file
sink and the events table.
"""

import ast
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from shared.log import _install_stdlib_intercept

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reset_uvicorn_levels() -> Iterator[None]:
    yield
    logging.getLogger("uvicorn.access").setLevel(logging.NOTSET)
    logging.getLogger("uvicorn.error").setLevel(logging.NOTSET)


def test_uvicorn_error_traceback_reaches_loguru(loguru_records: list[dict]) -> None:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    """An ERROR record on `uvicorn.error` — the shape of an unhandled ASGI
    exception — flows through to loguru after `_install_stdlib_intercept`,
    which is the precondition for it landing in gateway.log + events."""
    _install_stdlib_intercept()
    try:
        raise RuntimeError("boom")  # noqa: TRY301 — set sys.exc_info() for the .exception() record
    except RuntimeError:
        logging.getLogger("uvicorn.error").exception(
            "Exception in ASGI application\nTraceback (most recent call last):"
        )
    assert any(
        r["level"].name == "ERROR" and "Exception in ASGI application" in r["message"]  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        for r in loguru_records  # pyright: ignore[reportUnknownVariableType]
    )


def test_uvicorn_error_startup_info_reaches_loguru(loguru_records: list[dict]) -> None:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    """INFO startup lines ("Uvicorn running on ...", "Started server process")
    still pass — they are the pane-scrollback staples operators grep for."""
    _install_stdlib_intercept()
    logging.getLogger("uvicorn.error").info("Uvicorn running on http://127.0.0.1:8000")
    assert any(
        r["level"].name == "INFO" and "Uvicorn running on" in r["message"]  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        for r in loguru_records  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType, reportUnknownVariableType]
    )


def test_uvicorn_access_info_gated_away(loguru_records: list[dict]) -> None:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    """Per-request INFO on `uvicorn.access` is gated to WARNING — a busy
    gateway would otherwise emit thousands of access lines into the file
    sink and the events table every day."""
    _install_stdlib_intercept()
    logging.getLogger("uvicorn.access").info('GET /api/health HTTP/1.1" 200')
    assert not any(
        r["level"].name == "INFO" and "/api/health" in r["message"]  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        for r in loguru_records  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType, reportUnknownVariableType]
    )
    # WARNING+ still passes (e.g. a record with an over-long request line).
    logging.getLogger("uvicorn.access").warning("ASGI access record warning marker")
    assert any(
        r["level"].name == "WARNING" and "access record warning" in r["message"]  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        for r in loguru_records  # pyright: ignore[reportUnknownVariableType]
    )


def test_gateway_uvicorn_run_passes_log_config_none() -> None:
    """The gateway's `uvicorn.run` call must stay `log_config=None` — the
    whole point of #970. Guarded statically so a future edit cannot silently
    reintroduce uvicorn's dictConfig clobber."""
    src = (_REPO_ROOT / "gateway" / "_server.py").read_text()
    tree = ast.parse(src)
    found: list[ast.keyword] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "uvicorn"
        ):
            found = [k for k in node.keywords if k.arg == "log_config"]
            break
    assert found, "uvicorn.run call not found in gateway/_server.py"
    assert len(found) == 1
    assert isinstance(found[0].value, ast.Constant) and found[0].value.value is None
