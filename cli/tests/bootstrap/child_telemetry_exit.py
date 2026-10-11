"""Child that dispatches one CLI verb and lets interpreter shutdown drain telemetry.

Usage: `child_telemetry_exit.py <success|exception|empty|unavailable>`. Receiver and
OTel providers are real; only data persistence is isolated.
"""

import argparse
import sys

from base import telemetry
from base.telemetry import emitter, event_store
from base.telemetry.metrics import observed_metrics
from cli import main, parsers

# A long batch interval makes first initialization at exit deterministic without sleeps.
emitter._FLUSH_INTERVAL_S = 60  # pyright: ignore[reportAttributeAccessIssue]


def _discard(*_args: object) -> None:
    return None


event_store.store_events = _discard  # pyright: ignore[reportAttributeAccessIssue]
observed_metrics.project_events = _discard  # pyright: ignore[reportAttributeAccessIssue]


def dispatch(args: argparse.Namespace) -> int:
    if sys.argv[1] != "empty":
        telemetry.init_telemetry(process="cli-health-probe", pipeline=args.producer())
        telemetry.emit("telemetry", "health_probe_ran", attributes={"unhealthy_checks": 0})
    if sys.argv[1] == "exception":
        raise RuntimeError("handler failure")
    return 7


parser = argparse.ArgumentParser()
parser.set_defaults(func=dispatch)


def _build_parser(*, retained_children: object) -> argparse.ArgumentParser:
    del retained_children
    return parser


parsers.build_parser = _build_parser  # pyright: ignore[reportAttributeAccessIssue]
try:
    result = main.main([])
except RuntimeError as exc:
    assert str(exc) == "handler failure"
else:
    assert result == 7
